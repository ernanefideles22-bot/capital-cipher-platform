"""Dedicated Gate Testnet composition root; no PAPER staging controls are relaxed.

Connects research -> central risk -> durable OMS -> the protected Gate adapter.
Writes require a current exact-revision release: external-audit policy or the
separately acknowledged, bounded owner-controlled Gate virtual-funds policy.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from app.agents import indicators
from app.audit.service import AuditService
from app.core.errors import SecurityError
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.session import Database
from app.database.repositories.repository import Repository
from app.demo.gate_market import fetch_candles
from app.demo.gate_testnet_transport import GateTestnetTransport, TestnetCredentials
from app.execution.adapters.gate_testnet import GateTestnetExecutionAdapter, number
from app.execution.adapters.gate_testnet_metadata import GateTestnetMetadata
from app.execution.gate_protection_store import GateProtectionStore
from app.execution.gate_equity import GateEquityBaseline
from app.execution.gate_demo_release import GateDemoOwnerGrant, GateDemoRelease
from app.market_data.store import CandleStore
from app.oms.service import OMSService
from app.oms.reconciliation import ReconciliationService
from app.paper_trading.engine import PaperTradingEngine
from app.release_readiness.service import ReleaseReadinessService
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.common import Exchange, CandidateAction
from app.schemas.decisions import Decision
from app.schemas.market import Candle
from app.schemas.oms import ExecutionEnvironment, ReconciliationRunStatus
from app.schemas.risk import RiskLimits


class GateAccountLease:
    """One PostgreSQL session lock per actual Gate account, across API keys."""

    def __init__(self, database, account_id):
        if (
            isinstance(account_id, bool)
            or not str(account_id).isdecimal()
            or int(account_id) <= 0
        ):
            raise SecurityError("Gate account identity unavailable")
        self._database = database
        self._namespace = 15471612
        self._key = (
            int.from_bytes(
                hashlib.sha256(("GATE:USDT:" + str(account_id)).encode()).digest()[:4],
                "big",
            )
            & 0x7FFFFFFF
        )
        self._connection = None

    async def acquire(self):
        if self._connection is not None:
            raise SecurityError("Gate lease already held")
        conn = await self._database.engine.connect()
        try:
            held = await conn.scalar(
                text("SELECT pg_try_advisory_lock(:ns,:key)"),
                {"ns": self._namespace, "key": self._key},
            )
            await conn.commit()
            if held is not True:
                raise SecurityError("Another Gate executor owns the account")
            self._connection = conn
        except BaseException:
            await conn.close()
            raise

    async def valid(self):
        if self._connection is None:
            return False
        try:
            held = await self._connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory' AND classid=:ns AND objid=:key AND objsubid=2)"
                ),
                {"ns": self._namespace, "key": self._key},
            )
            await self._connection.commit()
            return held is True
        except Exception:
            return False

    async def close(self):
        conn, self._connection = self._connection, None
        if conn is None:
            return
        try:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:ns,:key)"),
                {"ns": self._namespace, "key": self._key},
            )
            await conn.commit()
        except Exception:
            await conn.invalidate()
        finally:
            await conn.close()


class GateOMSConnection:
    def __init__(
        self,
        *,
        repository,
        risk,
        oms,
        adapter,
        reconciliation,
        audit,
        journal,
        metadata,
        lease,
        release_guard,
        writes_enabled,
        candle_store,
        market_fetch=fetch_candles,
        clock=time.time,
        demo_release=None,
    ):
        self.repository, self.risk, self.oms = repository, risk, oms
        self.adapter, self.reconciliation, self.audit = adapter, reconciliation, audit
        self.journal, self.metadata, self.lease = journal, metadata, lease
        self.release_guard, self.writes_enabled = release_guard, writes_enabled is True
        self.candle_store, self.market_fetch, self.clock = (
            candle_store,
            market_fetch,
            clock,
        )
        self._lock = asyncio.Lock()
        self.status = "CONNECTED_READ_ONLY"
        self.last_order_id = None
        self.release_authorized = False
        self.central_risk_clear = False
        self.analysis_stage = "IDLE"
        self.demo_release = demo_release

    async def entry_authorized(self):
        control = await self.repository.load_risk_control_state()
        self.central_risk_clear = bool(
            control is not None
            and control.active is False
            and not self.risk.kill_switch_active
        )
        self.release_authorized = await self.release_guard() is True
        return (
            self.writes_enabled
            and await self.lease.valid()
            and self.central_risk_clear
            and self.release_authorized
        )

    async def exit_authorized(self):
        # Risk-reducing exits remain possible after an expired release or kill.
        control = await self.repository.load_risk_control_state()
        return self.writes_enabled and await self.lease.valid() and control is not None

    async def reconcile(self):
        run = await self.reconciliation.reconcile_once()
        if run.status != ReconciliationRunStatus.MATCHED:
            raise SecurityError("Gate OMS reconciliation did not match")
        return run

    async def process_analysis(self, result):
        async with self._lock:
            self.analysis_stage = "CONTROL_CHECK"
            if not await self.entry_authorized():
                self.status = "ENTRY_BLOCKED"
                return {"status": self.status}
            if self.demo_release is not None and not self.demo_release.may_queue():
                self.status = (
                    "CANARY_BUDGET_CONSUMED"
                    if self.demo_release.grant.mode == "CANARY_ONLY"
                    else "ENTRY_BLOCKED"
                )
                return {"status": self.status}
            if self.journal.recovery_status()["recovery_required"]:
                self.status = "POSITION_OR_RECOVERY_PENDING"
                return {"status": self.status}
            if (
                result.get("exchange") != "GATE"
                or result.get("market") != "GATE_USDT_FUTURES_TESTNET"
                or result.get("analysis_scope") != "RESEARCH_ONLY"
                or result.get("execution_enabled") is not False
                or result.get("source") != "AGENTS"
            ):
                raise SecurityError("Gate research provenance rejected")
            decision = Decision.model_validate(result["decision"])
            if decision.symbol != "BTCUSDT" or decision.timeframe != "15m":
                raise SecurityError("Initial Gate execution scope is BTCUSDT 15m")
            created = datetime.fromisoformat(result["created_at"])
            candle = datetime.fromisoformat(result["candle_closed_at"])
            if created.tzinfo is None or candle.tzinfo is None:
                raise SecurityError("Gate analysis timestamps require timezone")
            if (
                not 0 <= self.clock() - created.timestamp() <= 60
                or not 0 <= self.clock() - candle.timestamp() <= 960
            ):
                raise SecurityError("Gate agent signal is stale")
            if decision.candidate_action not in {
                CandidateAction.BUY,
                CandidateAction.SELL,
            }:
                self.status = "WAITING_FOR_SIGNAL"
                return {"status": self.status}
            key = f"gate-agents:{decision.symbol}:{decision.timeframe}:{candle.isoformat()}"
            if (
                await self.repository.load_central_risk_evaluation_by_key(key)
                is not None
            ):
                return {"status": "ALREADY_EVALUATED"}
            self.analysis_stage = "RECONCILE"
            await self.reconcile()
            self.analysis_stage = "REFRESH_RISK"
            await self.risk.refresh_positions()
            # Independently refresh Gate candles and mark, never reuse the
            # credential-free research process's advisory risk approval.
            self.analysis_stage = "MARKET_REFRESH"
            rows = await self.market_fetch(
                decision.symbol, decision.timeframe, limit=200
            )
            candles = [
                Candle(
                    exchange=Exchange.GATE,
                    symbol=decision.symbol,
                    timeframe=decision.timeframe,
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    closed_at=datetime.fromtimestamp(
                        r["close_time_ms"] / 1000, timezone.utc
                    ),
                )
                for r in rows
            ]
            if len(candles) < 60 or candles[-1].closed_at != candle:
                raise SecurityError("Gate signal candle no longer matches market data")
            existing = self.candle_store.latest(
                "GATE", decision.symbol, decision.timeframe
            )
            for item in candles:
                if existing is None or item.closed_at > existing.closed_at:
                    self.candle_store.add(item)
            metadata, observed = await self.metadata(decision.symbol)
            mark, tick = number(metadata["mark_price"]), number(
                metadata["order_price_round"]
            )
            if (
                not 0 <= number(self.clock()) - number(observed) <= 10
                or min(mark, tick) <= 0
            ):
                raise SecurityError("Gate market metadata invalid")
            atr = indicators.atr(candles)
            if atr is None or atr <= 0:
                raise SecurityError("Gate ATR unavailable")
            distance = number(atr) * Decimal("1.5")
            stop = (
                ((mark - distance) / tick).to_integral_value(rounding=ROUND_FLOOR)
                * tick
                if decision.candidate_action == CandidateAction.BUY
                else ((mark + distance) / tick).to_integral_value(
                    rounding=ROUND_CEILING
                )
                * tick
            )
            adjusted_atr = float(abs(mark - stop) / Decimal("1.5"))
            stable = str(uuid5(NAMESPACE_URL, key))
            decision = decision.model_copy(
                update={
                    "decision_id": stable,
                    "correlation_id": stable,
                    "created_at": candle,
                }
            )
            await self.repository.save_decision(decision)
            self.analysis_stage = "RISK_CHECK"
            check = await self.risk.check(
                decision,
                entry_price=float(mark),
                atr=adjusted_atr,
                leverage=1,
                max_notional_override=50,
                idempotency_key=key,
            )
            if not check.approved:
                self.status = "RISK_REJECTED"
                return {"status": self.status, "risk_check_id": check.risk_check_id}
            self.analysis_stage = "OMS_QUEUE"
            order = await self.oms.submit_approved(
                decision, check, current_price=float(mark)
            )
            if self.demo_release is not None:
                self.demo_release.reserve(order)
            self.analysis_stage = "QUEUED"
            self.last_order_id = order.oms_order_id
            self.status = "QUEUED"
            return {"status": self.status, "oms_order_id": order.oms_order_id}

    async def tick(self):
        async with self._lock:
            if not await self.lease.valid():
                self.status = "LEASE_UNAVAILABLE"
                raise SecurityError("Gate account lease lost")
            if not self.writes_enabled:
                await self.entry_authorized()
                self.status = "CONNECTED_READ_ONLY"
                return
            await self.entry_authorized()
            cycles = self.journal.recovery_status()["cycles"]
            if cycles:
                if len(cycles) != 1 or cycles[0]["state"] != "PROTECTED":
                    self.status = "RECOVERY_REQUIRED"
                    return
                order = await self.repository.load_oms_order(cycles[0]["id"])
                if order is None:
                    raise SecurityError("Gate protected cycle has no OMS order")
                state = await self.adapter.reconcile_protected_order(order)
                if state["state"] == "PROTECTED":
                    if (
                        self.demo_release is not None
                        and self.demo_release.grant.mode == "CANARY_ONLY"
                    ):
                        self.demo_release.record_protected(
                            order, await self.adapter.capture_canary_protection(order)
                        )
                        state = await self.adapter.close_protected_order(order)
                        state["cause"] = "OWNER_CANARY"
                    else:
                        state = await self._monitor_protected(order, state)
                if state["state"] == "CLOSED":
                    await self.audit.record(
                        correlation_id=order.correlation_id,
                        audit_type="OMS_GATE_EXIT_VERIFIED",
                        entity_type="oms_order",
                        entity_id=order.oms_order_id,
                        payload=state,
                    )
                await self.reconcile()
                if (
                    state["state"] == "CLOSED"
                    and self.demo_release is not None
                    and self.demo_release.grant.mode == "CANARY_ONLY"
                ):
                    await self._record_canary_close(order)
                    self.status = "AWAITING_OWNER_CANARY_ACCEPTANCE"
                else:
                    self.status = state["state"]
                return
            if (
                self.demo_release is not None
                and self.demo_release.grant.mode == "CANARY_ONLY"
            ):
                slot = self.demo_release.slot()
                if slot is None:
                    self.status = "WAITING_FOR_CANARY_SIGNAL"
                    return
                row = self.journal.db.execute(
                    "SELECT state FROM cycles WHERE id=?", (slot,)
                ).fetchone()
                if row is not None:
                    # Never dispatch a second canary after no-fill, successful
                    # close or crash. Recovery can only collect existing reads.
                    owned_exit = self.journal.db.execute(
                        "SELECT 1 FROM exit_identity WHERE cycle_id=?", (slot,)
                    ).fetchone()
                    if not owned_exit:
                        self.status = "CANARY_UNFILLED"
                        return
                    if (
                        row[0] == "CLOSED"
                        and not self.demo_release.status()["canary_evidence_sha256"]
                    ):
                        order = await self.repository.load_oms_order(slot)
                        await self._record_canary_close(order)
                    self.status = "AWAITING_OWNER_CANARY_ACCEPTANCE"
                    return
            if not await self.entry_authorized():
                self.status = "ENTRY_BLOCKED"
                return
            await self.oms.dispatch_once()
            await self.reconcile()
            self.status = (
                "CANARY_VALIDATING"
                if self.demo_release is not None
                and self.demo_release.grant.mode == "CANARY_ONLY"
                else "OPERATIONAL"
            )

    async def _record_canary_close(self, order):
        run = await self.reconcile()
        control = await self.repository.load_risk_control_state()
        if (
            control is None
            or control.active
            or self.risk.kill_switch_active
            or self.risk.position_exposures()
        ):
            raise SecurityError("Gate canary has unresolved central risk")
        payload = await self.adapter.capture_canary_close(order)
        payload.update(
            reconciliation_run_id=run.run_id,
            reconciliation_status=run.status.value,
            risk_clear=True,
        )
        sha = self.demo_release.record_closed(order, payload)
        await self.audit.record(
            correlation_id=order.correlation_id,
            audit_type="GATE_DEMO_CANARY_EVIDENCE_VERIFIED",
            entity_type="oms_order",
            entity_id=order.oms_order_id,
            payload={
                "canary_sha256": sha,
                "entry_order_id": payload["entry_order_id"],
                "stop_id": payload["stop_id"],
                "exit_order_id": str(
                    payload["exit"].get("id_string") or payload["exit"]["id"]
                ),
                "reconciliation_run_id": run.run_id,
                "automatic_agents_enabled": False,
            },
        )
        protected = json.loads(
            self.demo_release.db.execute(
                "SELECT payload FROM gate_demo_protected_observations WHERE order_id=?",
                (order.oms_order_id,),
            ).fetchone()[0]
        )
        logging.getLogger("uvicorn.error").info(
            "GATE_DEMO_CANARY_VERIFIED %s",
            json.dumps(
                {
                    "sha256": sha,
                    "entry_order_id": payload["entry_order_id"],
                    "stop_id": payload["stop_id"],
                    "exit_order_id": str(
                        payload["exit"].get("id_string") or payload["exit"]["id"]
                    ),
                    "stop_observed_at": protected["stop_observed_at"],
                    "exit_observed_at": payload["exit_observed_at"],
                    "reconciliation_run_id": run.run_id,
                    "automatic_agents_enabled": False,
                },
                sort_keys=True,
            ),
        )

    async def _monitor_protected(self, order, state):
        proof = await self.adapter._protection.verify(order)
        metadata, observed = await self.metadata(order.symbol)
        if not 0 <= number(self.clock()) - number(observed) <= 10:
            raise SecurityError("Gate exit quote stale")
        mark = number(metadata["mark_price"])
        target = number(proof["take_profit"])
        reached = mark >= target if order.side.value == "BUY" else mark <= target
        # Close before stop expiration; no cancel-and-replace gap.
        if (
            reached
            or state["expires_at"] - self.clock() <= 180
            or not await self.entry_authorized()
        ):
            state = await self.adapter.close_protected_order(order)
            state["cause"] = "TARGET" if reached else "SAFETY"
        return state

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                self.status = "HALTED"
                if self.writes_enabled and not self.risk.kill_switch_active:
                    await self.risk.trigger_kill_switch(
                        reason="Gate flow failed: " + type(exc).__name__,
                        actor="gate-oms",
                    )
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except TimeoutError:
                pass


@asynccontextmanager
async def connect_gate_oms(values, journal, *, mount_verified):
    if values.get("GATE_OMS_CONNECTION_ENABLED") != "true":
        yield None
        return
    if not mount_verified or journal is None:
        raise SecurityError("Gate requires its existing durable cycle volume")
    url = values.get("DATABASE_URL", "")
    if not url.startswith("postgresql+asyncpg://"):
        raise SecurityError("Gate central OMS requires PostgreSQL runtime credentials")
    revision = values.get(
        "RAILWAY_GIT_COMMIT_SHA", values.get("APP_SOURCE_REVISION", "")
    )
    if len(revision) != 40:
        raise SecurityError("Gate source revision unavailable")
    enabled = values.get("GATE_OMS_WRITES_ENABLED") == "true"
    ack = values.get("GATE_OMS_ACKNOWLEDGEMENT", "")
    if enabled and ack != "GATE_TESTNET_VIRTUAL_FUNDS_ONLY":
        raise SecurityError("Gate virtual-funds acknowledgement required")
    db = Database(url, pool_size=3, max_overflow=0)
    transport = GateTestnetTransport(
        TestnetCredentials(
            values.get("CAPITAL_CIPHER_GATE_TESTNET_API_KEY", ""),
            values.get("CAPITAL_CIPHER_GATE_TESTNET_API_SECRET", ""),
        ),
        trusted_clock=time.time,
        writes_enabled=enabled,
        acknowledgement=ack,
    )
    metadata = GateTestnetMetadata()
    lease = None
    try:
        await db.verify_schema()
        account = await transport.account()
        if account.get("currency") != "USDT":
            raise SecurityError("Gate settlement rejected")
        lease = GateAccountLease(db, account.get("user"))
        binding = hashlib.sha256(
            ("GATE:USDT:" + str(account["user"])).encode()
        ).hexdigest()
        journal.db.execute(
            "CREATE TABLE IF NOT EXISTS gate_account_binding(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL)"
        )
        journal.db.execute(
            "INSERT OR IGNORE INTO gate_account_binding VALUES (1,?)", (binding,)
        )
        if (
            journal.db.execute(
                "SELECT fingerprint FROM gate_account_binding WHERE id=1"
            ).fetchone()[0]
            != binding
        ):
            raise SecurityError("Gate journal belongs to a different account")
        await lease.acquire()
        repository = Repository(db)
        audit = AuditService(repository=repository)
        sm = SystemStateMachine()
        await sm.transition(
            SystemState.INITIALIZING,
            reason="Gate virtual-funds execution",
            actor="gate-oms",
        )
        await sm.transition(
            SystemState.PAPER, reason="No real-funds execution state", actor="gate-oms"
        )
        store = CandleStore()
        risk = VenueAwareRiskManager(
            RiskLimits(
                risk_per_trade_percent=0.1,
                max_open_positions=1,
                default_leverage=1,
                max_leverage=1,
            ),
            sm,
            audit,
            repository=repository,
            candle_store=store,
            execution_environment=ExecutionEnvironment.TESTNET,
            testnet_exchange=Exchange.GATE,
            equity_baseline_store=GateEquityBaseline(journal),
        )
        release = ReleaseReadinessService(repository)
        demo_release = None
        if values.get("GATE_DEMO_OWNER_GRANT_JSON"):
            grant = GateDemoOwnerGrant.model_validate_json(
                values["GATE_DEMO_OWNER_GRANT_JSON"]
            )
            demo_release = GateDemoRelease(
                journal, grant, source_revision=revision, account_binding=binding
            )

        async def released():
            if demo_release is not None:
                return await demo_release.authorized()
            return await release.runtime_authorized(revision)

        holder = {}

        async def guard():
            return await holder["flow"].entry_authorized()

        async def exit_guard():
            return await holder["flow"].exit_authorized()

        adapter = GateTestnetExecutionAdapter(
            metadata,
            time.time,
            transport=transport,
            journal=journal,
            protection_store=GateProtectionStore(db),
            account_guard=guard,
            exit_guard=exit_guard,
            execution_enabled=enabled,
            entry_order_guard=(
                demo_release.authorize_order if demo_release is not None else None
            ),
        )
        risk.set_testnet_equity_provider(adapter.fetch_account_equity)
        await risk.initialize()
        oms = OMSService(
            target_environment=ExecutionEnvironment.TESTNET,
            target_exchange=Exchange.GATE,
            paper_engine=PaperTradingEngine(audit, risk, repository=repository),
            risk_manager=risk,
            audit_service=audit,
            adapters={(Exchange.GATE, ExecutionEnvironment.TESTNET): adapter},
            repository=repository,
            worker_id="gate-testnet-oms",
            lease_seconds=60,
            release_guard=released,
        )
        await oms.initialize()
        reconciliation = ReconciliationService(
            adapter=adapter,
            risk_manager=risk,
            audit_service=audit,
            repository=repository,
        )
        flow = GateOMSConnection(
            repository=repository,
            risk=risk,
            oms=oms,
            adapter=adapter,
            reconciliation=reconciliation,
            audit=audit,
            journal=journal,
            metadata=metadata,
            lease=lease,
            release_guard=released,
            writes_enabled=enabled,
            candle_store=store,
            demo_release=demo_release,
        )
        holder["flow"] = flow
        yield flow
    finally:
        if lease is not None:
            await lease.close()
        await transport.close()
        await metadata.aclose()
        await db.dispose()

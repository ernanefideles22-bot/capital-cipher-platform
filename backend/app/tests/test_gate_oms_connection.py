"""Research -> real central risk/OMS/repository -> simulated protected exchange."""

import os
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest
from app.audit.service import AuditService
from app.core.errors import SecurityError
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.session import Database
from app.database.repositories.repository import Repository
from app.database.models import RiskControlStateModel
from app.demo.gate_cycle_journal import GateCycleJournal
from app.execution.adapters.gate_testnet import GateTestnetExecutionAdapter
from app.execution.gate_runtime import (
    GateOMSConnection,
    GateAccountLease,
    connect_gate_oms,
)
from app.execution.gate_equity import GateEquityBaseline
from app.execution.gate_protection_validation import validate_gate_protection
from app.market_data.store import CandleStore
from app.oms.service import OMSService
from app.oms.reconciliation import ReconciliationService
from app.paper_trading.engine import PaperTradingEngine
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.risk import RiskLimits
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment, OMSOrderStatus
from app.tests.test_gate_testnet_execution import (
    Venue,
    order,
    setup,
)  # fixture and protocol simulator

NOW = int(time.time())
STAMP = datetime.fromtimestamp(NOW - 1, timezone.utc)


def analysis(action="BUY"):
    return dict(
        exchange="GATE",
        market="GATE_USDT_FUTURES_TESTNET",
        analysis_scope="RESEARCH_ONLY",
        execution_enabled=False,
        source="AGENTS",
        created_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
        candle_closed_at=STAMP.isoformat(),
        decision=dict(
            correlation_id="research",
            symbol="BTCUSDT",
            timeframe="15m",
            candidate_action=action,
            confidence=90,
            strategy="SCALP_15M",
        ),
    )


@pytest.fixture
async def flow(tmp_path):
    db = Database(f'sqlite+aiosqlite:///{tmp_path/"oms.db"}')
    await db.create_all()
    repo = Repository(db)
    async with db.session() as session, session.begin():
        session.add(
            RiskControlStateModel(
                singleton_id=1,
                active=False,
                revision=0,
                updated_at=datetime.now(timezone.utc),
            )
        )
    journal = GateCycleJournal(tmp_path / "cycles.db")

    def current(_id):
        cycle = journal.db.execute(
            "SELECT id FROM cycles ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        return journal.read(cycle)

    class SimVenue(Venue):
        async def place_stop(self, **kwargs):
            result = await super().place_stop(**kwargs)
            self.stop["create_time"] = NOW
            return result

        async def rows(self, kind, **kwargs):
            if kind == "stops":
                return (
                    [self.stop.copy()]
                    if self.stop and self.stop["status"] == "open"
                    else []
                )
            if kind == "fills" and kwargs.get("contract") == "BTC_USDT":
                fills = []
                if "11" in self.orders:
                    size = int(self.orders["11"]["size"])
                    fills.append(
                        dict(
                            id="101",
                            order_id="11",
                            text=self.orders["11"]["text"],
                            size=size,
                            price="80000",
                            fee=".01",
                            contract="BTC_USDT",
                            create_time=NOW,
                        )
                    )
                    if "33" in self.orders:
                        fills.append(
                            dict(
                                id="102",
                                order_id="33",
                                text=self.orders["33"]["text"],
                                size=-size,
                                price="81000",
                                fee=".01",
                                contract="BTC_USDT",
                                create_time=NOW,
                            )
                        )
                return fills
            return await super().rows(kind, **kwargs)

    venue = SimVenue(SimpleNamespace(read=current))
    mark = {"value": "80000"}

    async def metadata(symbol):
        return (
            dict(
                name=symbol.replace("USDT", "_USDT"),
                quanto_multiplier=".0001",
                order_size_min="1",
                order_size_max="10000",
                in_delisting=False,
                mark_price=mark["value"],
                last_price=mark["value"],
                order_price_round=".1",
            ),
            NOW,
        )

    async def market(*args, **kwargs):
        return [
            dict(
                open=80000,
                high=80100,
                low=79900,
                close=80000,
                volume=100,
                close_time_ms=(STAMP.timestamp() - (199 - i) * 900) * 1000,
            )
            for i in range(200)
        ]

    class Protection:
        async def verify(self, oms_order):
            key = f"gate-agents:BTCUSDT:15m:{STAMP.isoformat()}"
            if oms_order.strategy == "GATE_TECHNICAL_CANARY":
                release = holder["flow"].demo_release
                key = f"gate-technical-canary:{release.binding}:{release.grant.source_revision}"
            stored = await repo.load_central_risk_evaluation_by_key(key)
            check, _ = stored
            return validate_gate_protection(
                oms_order,
                dict(
                    oms_order_id=oms_order.oms_order_id,
                    approval_id=oms_order.approval_id,
                    exchange="GATE",
                    environment="TESTNET",
                    symbol="BTCUSDT",
                    side=oms_order.side.value,
                    execution_intent="ENTRY",
                    reduce_only=False,
                    stop_loss=check.stop_loss,
                    take_profit=check.take_profit,
                    leverage=1,
                    reference_price=oms_order.reference_price,
                ),
            )

    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="test", actor="test")
    await sm.transition(SystemState.PAPER, reason="test", actor="test")
    audit = AuditService(repository=repo)
    store = CandleStore()
    risk = VenueAwareRiskManager(
        RiskLimits(risk_per_trade_percent=0.1, max_open_positions=1, max_leverage=1),
        sm,
        audit,
        repository=repo,
        candle_store=store,
        execution_environment=ExecutionEnvironment.TESTNET,
        testnet_exchange=Exchange.GATE,
        equity_baseline_store=GateEquityBaseline(journal),
    )
    release = {"allowed": True}

    async def released():
        return release["allowed"]

    async def leased():
        return True

    holder = {}

    async def guard():
        return await holder["flow"].entry_authorized()

    async def exit_guard():
        return await holder["flow"].exit_authorized()

    adapter = GateTestnetExecutionAdapter(
        metadata,
        lambda: NOW,
        transport=venue,
        journal=journal,
        protection_store=Protection(),
        account_guard=guard,
        exit_guard=exit_guard,
        execution_enabled=True,
    )
    risk.set_testnet_equity_provider(adapter.fetch_account_equity)
    await risk.initialize()
    oms = OMSService(
        target_environment=ExecutionEnvironment.TESTNET,
        target_exchange=Exchange.GATE,
        paper_engine=PaperTradingEngine(audit, risk, repository=repo),
        risk_manager=risk,
        audit_service=audit,
        adapters={(Exchange.GATE, ExecutionEnvironment.TESTNET): adapter},
        repository=repo,
        release_guard=released,
    )
    reconcile = ReconciliationService(
        adapter=adapter, risk_manager=risk, audit_service=audit, repository=repo
    )
    connection = GateOMSConnection(
        repository=repo,
        risk=risk,
        oms=oms,
        adapter=adapter,
        reconciliation=reconcile,
        audit=audit,
        journal=journal,
        metadata=metadata,
        lease=SimpleNamespace(valid=leased),
        release_guard=released,
        writes_enabled=True,
        candle_store=store,
        market_fetch=market,
        clock=lambda: NOW,
    )
    holder["flow"] = connection
    yield connection, venue, mark, release
    journal.close()
    await db.dispose()


@pytest.mark.asyncio
async def test_real_risk_oms_queue_entry_stop_target_exit_and_flat_risk(flow):
    connection, venue, mark, _ = flow
    result = await connection.process_analysis(analysis())
    assert result["status"] == "QUEUED"
    assert not venue.writes  # analysis never sends directly
    assert (await connection.process_analysis(analysis()))[
        "status"
    ] == "ALREADY_EVALUATED"
    await connection.tick()
    assert venue.writes == ["entry", "stop"]
    stored = await connection.repository.load_oms_order(result["oms_order_id"])
    assert stored.status == OMSOrderStatus.FILLED
    assert connection.risk.position_exposures()
    mark["value"] = "81000"
    await connection.tick()
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]
    assert connection.journal.read(stored.oms_order_id)["state"] == "CLOSED"
    assert not connection.risk.position_exposures()  # no resurrected Gate entry risk
    fills = await connection.repository.load_execution_fills(limit=100)
    assert {f.venue_order_id for f in fills} == {"11", "33"}
    assert all(f.oms_order_id == stored.oms_order_id for f in fills)
    assert {f.side.value for f in fills} == {"BUY", "SELL"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["release", "disabled", "kill"])
async def test_controls_block_agent_submission_without_touching_exchange(flow, kind):
    connection, venue, _, release = flow
    if kind == "release":
        release["allowed"] = False
    if kind == "disabled":
        connection.writes_enabled = False
    if kind == "kill":
        await connection.risk.trigger_kill_switch(reason="test", actor="test")
    assert (await connection.process_analysis(analysis()))["status"] == "ENTRY_BLOCKED"
    await connection.tick()
    assert not venue.writes


@pytest.mark.asyncio
async def test_wait_is_not_replaced_by_forced_canary(flow):
    connection, venue, _, _ = flow
    assert (await connection.process_analysis(analysis("WAIT")))[
        "status"
    ] == "WAITING_FOR_SIGNAL"
    assert not venue.writes
    malicious = analysis()
    malicious["source"] = "OPERATIONAL_CANARY"
    with pytest.raises(SecurityError):
        await connection.process_analysis(malicious)
    assert not venue.writes


@pytest.mark.asyncio
async def test_expired_release_still_permits_owned_risk_reducing_exit(flow):
    connection, venue, _, release = flow
    result = await connection.process_analysis(analysis())
    await connection.tick()
    release["allowed"] = False
    await connection.tick()
    assert connection.journal.read(result["oms_order_id"])["state"] == "CLOSED"
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]


@pytest.mark.asyncio
async def test_stop_trigger_proves_generated_order_before_closing_cycle(setup):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    venue.stop.update(
        status="finished", finish_as="succeeded", trade_id=33, finish_time=1001
    )
    venue.size = 0
    venue.orders["33"] = dict(
        id="33",
        text="api",
        contract="BTC_USDT",
        size=0,
        left=0,
        status="finished",
        finish_as="filled",
        is_close=True,
        is_reduce_only=True,
        is_liq=False,
        fill_price="79000",
    )
    original = venue.rows

    async def rows(kind, **kwargs):
        return [] if kind == "stops" else await original(kind, **kwargs)

    venue.rows = rows
    result = await adapter.reconcile_protected_order(order())
    assert result["state"] == "CLOSED" and result["cause"] == "STOP"
    assert journal.read("order-0001")["state"] == "CLOSED"
    assert venue.writes == [
        "entry",
        "stop",
    ]  # no second close or blind stop cancellation


@pytest.mark.asyncio
async def test_flat_position_without_stop_execution_evidence_does_not_release_slot(
    setup,
):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    venue.size = 0
    venue.stop.update(
        status="finished", finish_as="expired", trade_id=0, finish_time=1001
    )
    with pytest.raises(SecurityError):
        await adapter.reconcile_protected_order(order())
    assert journal.read("order-0001")["state"] == "HALTED"


def test_equity_drawdown_baselines_survive_restart(tmp_path):
    path = tmp_path / "journal.db"
    journal = GateCycleJournal(path)
    store = GateEquityBaseline(journal)
    today = datetime.now(timezone.utc).date()
    store.observe(today, 1000)
    store.observe(today, 1100)
    journal.close()
    journal = GateCycleJournal(path)
    try:
        result = GateEquityBaseline(journal).observe(today, 950)
        assert result == dict(initial=1000, peak=1100, daily_initial=1000)
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_connection_default_does_not_read_credentials_or_database():
    async with connect_gate_oms({}, None, mount_verified=False) as connection:
        assert connection is None
    with pytest.raises(SecurityError):
        async with connect_gate_oms(
            {"GATE_OMS_CONNECTION_ENABLED": "true"}, None, mount_verified=False
        ):
            pass


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="PostgreSQL integration URL unavailable",
)
async def test_postgres_account_lease_excludes_second_writer_and_releases():
    db = Database(os.environ["POSTGRES_TEST_URL"], pool_size=3)
    first, second = GateAccountLease(db, 902763524), GateAccountLease(db, 902763524)
    try:
        await first.acquire()
        assert await first.valid()
        with pytest.raises(SecurityError):
            await second.acquire()
        await first.close()
        assert not await first.valid()
        await second.acquire()
        assert await second.valid()
    finally:
        await first.close()
        await second.close()
        await db.dispose()


@pytest.mark.asyncio
async def test_read_only_switch_never_closes_existing_protected_position(flow):
    connection, venue, _, _ = flow
    await connection.process_analysis(analysis())
    await connection.tick()
    connection.writes_enabled = False
    await connection.tick()
    assert venue.writes == ["entry", "stop"] and venue.size != 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("trade_id", 44), ("finish_time", 5000), ("finish_as", "failed")]
)
async def test_generated_stop_identity_and_time_mismatch_cannot_release_slot(
    setup, field, value
):
    adapter, venue, journal = setup
    await adapter.submit_order(order())
    venue.stop.update(
        status="finished", finish_as="succeeded", trade_id=33, finish_time=1001
    )
    venue.size = 0
    venue.orders["33"] = dict(
        id="33",
        text="api",
        contract="BTC_USDT",
        size=0,
        left=0,
        status="finished",
        finish_as="filled",
        is_close=True,
        is_reduce_only=True,
        is_liq=False,
        fill_price="79000",
    )
    venue.stop[field] = value
    original = venue.rows

    async def rows(kind, **kwargs):
        return [] if kind == "stops" else await original(kind, **kwargs)

    venue.rows = rows
    with pytest.raises((SecurityError, KeyError)):
        await adapter.reconcile_protected_order(order())
    assert journal.read("order-0001")["state"] != "CLOSED"
    assert venue.writes == ["entry", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_gate_worker_cannot_claim_or_expire_another_venue_command(flow, expired):
    from app.database.models import OMSOrderModel, ExecutionCommandModel
    from app.schemas.oms import ExecutionCommand, ExecutionCommandStatus

    connection, venue, _, _ = flow
    result = await connection.process_analysis(analysis())
    other = order(
        exchange=Exchange.BYBIT,
        oms_order_id="foreign-order",
        client_order_id="foreign-client",
        approval_id="b" * 64,
    )
    stamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    kwargs = dict(
        command_id="foreign-command",
        command_type="SUBMIT",
        oms_order_id=other.oms_order_id,
        available_at=stamp,
        created_at=stamp,
    )
    if expired:
        kwargs.update(
            status=ExecutionCommandStatus.LEASED,
            attempt_count=1,
            leased_by="other",
            lease_expires_at=stamp,
        )
    command = ExecutionCommand(**kwargs)
    async with connection.repository._db.session() as session, session.begin():
        session.add(OMSOrderModel(**connection.repository._oms_order_values(other)))
        session.add(
            ExecutionCommandModel(
                **connection.repository._execution_command_values(command)
            )
        )
    await connection.tick()
    assert venue.writes == ["entry", "stop"]
    assert (
        await connection.repository.load_oms_order(result["oms_order_id"])
    ).status == OMSOrderStatus.FILLED
    async with connection.repository._db.session() as session:
        foreign = await session.get(ExecutionCommandModel, command.command_id)
        assert (
            foreign.status == command.status.value
            and foreign.attempt_count == command.attempt_count
        )

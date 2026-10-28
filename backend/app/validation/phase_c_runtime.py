"""Synthetic PAPER acceptance probes. Never imported by the serving runtime.

Callers own new isolated databases. No exchange HTTP or production fixtures.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import AuditError, DatabaseError
from app.database.models import PaperOrderModel
from app.main import create_app
from app.schemas.decisions import Decision
from app.schemas.market import Candle
from app.validation.office_runtime_certification import probe_candles, seed_probe
from app.validation.paper_flow import fresh_fixture_close, trust_fixture_clock

DESKS = (("BTCUSDT", "15m"), ("ETHUSDT", "1h"), ("SOLUSDT", "4h"))
CRASH_BOUNDARIES = ("before_consume", "uncommitted_flush", "committed_unacknowledged", "book_registered")


@contextmanager
def no_exchange_http():
    async def blocked_async(_transport, request):
        raise httpx.ConnectError("Isolated PAPER probe; external HTTP disabled", request=request)
    def blocked_sync(_transport, request):
        raise httpx.ConnectError("Isolated PAPER probe; external HTTP disabled", request=request)
    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async), patch.object(
            httpx.HTTPTransport, "handle_request", blocked_sync):
        yield


async def prepared_closes(context):
    streams = [[c.model_copy(update={"symbol": symbol}) for c in probe_candles(tf)]
               for symbol, tf in DESKS]
    for rows in streams:
        await seed_probe(context, rows)
    trust_fixture_clock(context)
    return [fresh_fixture_close(rows[-1], rows[-2]) for rows in streams]


async def validate_three_desks(settings):
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert context.oms_service.target_environment.value == "PAPER"
        assert len(context.agent_registry.registrations()) == 300
        assert not await context.repository.load_paper_orders()
        closes = await prepared_closes(context)
        decisions = await asyncio.gather(*(context.orchestrator.on_candle_closed(c) for c in closes))
        assert all(d and d.risk_status.value in {"APPROVED", "REDUCED"} for d in decisions)
        assert {d.strategy for d in decisions} == {"SCALP_15M_v1", "DAY_1H_v1", "SWING_4H_v1"}
        orders = list(context.paper_engine.open_orders.values())
        assert len(orders) == context.risk_manager.state.open_positions == 3
        assert all(o.position_size <= 2000 for o in orders)
        gross = sum(o.position_size for o in orders)
        assert gross <= 6000
        timing = context.orchestrator.status()["paper_execution_timing"]
        assert timing["receipt_to_fill_ms"]["samples"] == 3
        assert all(row["samples"] == 1 for row in timing["by_timeframe"].values())
        assert {r["paper_order_id"] for r in timing["recent"]} == {o.paper_order_id for o in orders}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for order in orders:
                response = await client.get(f"/api/v1/paper/orders/{order.paper_order_id}")
                assert response.json()["data"]["order"] == order.model_dump(mode="json")
                chain = (await client.get(f"/api/v1/audit/correlation/{order.correlation_id}")).json()["data"]["chain"]
                assert {"DECISION_CANDIDATE", "RISK_CHECK", "PAPER_ORDER_CREATED"} <= {r["audit_type"] for r in chain}
        assert all(d is None for d in await asyncio.gather(*(context.orchestrator.on_candle_closed(c) for c in closes)))
        assert len(await context.repository.load_paper_orders()) == 3
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert set(restored.paper_engine.open_orders) == {o.paper_order_id for o in orders}
        assert restored.risk_manager.state.open_positions == 3
        assert restored.paper_engine.balance == 10_000
        assert restored.orchestrator.status()["paper_execution_timing"]["receipt_to_fill_ms"]["p95"] is None
    return {"desks": 3, "fills": 3, "gross": gross, "timing": timing,
            "replay_no_duplicate": True, "restored_positions": 3, "shared_balance": 10_000,
            "scope": "synthetic isolated PAPER; fixture caps 20%; not production latency"}


async def validate_concurrent_fault(settings, fault):
    if fault not in {"database", "broker", "audit"}:
        raise ValueError("PHASE_C_FAULT_REFUSED")
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    reached = 0
    async with app.router.lifespan_context(app):
        closes = await prepared_closes(context)
        async def unavailable(*args, **kwargs):
            nonlocal reached
            reached += 1
            raise (AuditError if fault == "audit" else DatabaseError)("Isolated Phase C fault")
        target, name = {"database": (context.repository, "save_candle"),
                        "broker": (context.event_bus, "publish"),
                        "audit": (context.audit_service, "record")}[fault]
        with patch.object(target, name, unavailable):
            results = await asyncio.gather(*(context.orchestrator.on_candle_closed(c) for c in closes))
        assert all(r is None for r in results)
        assert reached > 0
        assert not await context.repository.load_paper_orders()
        assert context.risk_manager.state.open_positions == 0
        assert context.orchestrator.status()["paper_execution_timing"]["receipt_to_fill_ms"]["samples"] == 0
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert not restored.paper_engine.open_orders
        assert restored.risk_manager.state.open_positions == 0
    return {"fault": fault, "concurrent_desks": 3, "boundary_reached": reached,
            "orders": 0, "rebuild_positions": 0, "injection": "explicit dependency exception; not network chaos"}


async def validate_capacity_conflict(settings, opposing):
    """Three independent pools and approvals compete, even with opposite sides."""
    from app.audit.service import AuditService
    from app.core.state_machine import SystemState, SystemStateMachine
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    from app.paper_trading.engine import PaperTradingEngine
    from app.risk.manager import RiskManager
    from app.schemas.paper import PaperOrder
    from app.schemas.risk import RiskLimits
    databases = [Database(settings.database_url, pool_size=1, max_overflow=0) for _ in range(3)]
    limits = RiskLimits(max_gross_exposure_percent=40, max_net_exposure_percent=40,
                        max_symbol_exposure_percent=40)
    ready, arrived = asyncio.Event(), 0
    plans = []
    try:
        for database, (_, timeframe), strategy in zip(databases, DESKS,
                ("SCALP_15M_v1", "DAY_1H_v1", "SWING_4H_v1")):
            repository = Repository(database)
            state = SystemStateMachine()
            await state.transition(SystemState.INITIALIZING, reason="isolated probe", actor="validation")
            await state.transition(SystemState.PAPER, reason="isolated probe", actor="validation")
            audit = AuditService(repository=repository)
            manager = RiskManager(limits, state, audit, repository=repository)
            await manager.initialize()
            engine = PaperTradingEngine(audit, manager, repository=repository)
            decision = Decision(correlation_id=str(uuid4()), symbol="BTCUSDT", timeframe=timeframe,
                strategy=strategy, candidate_action="SELL" if opposing and timeframe == "1h" else "BUY", confidence=80)
            await repository.save_decision(decision)
            check = await manager.check(decision, entry_price=100, atr=1, balance=10_000)
            assert check.approved and check.position_size == 4000
            consume = repository.consume_order_approval
            async def together(*args, _consume=consume, **kwargs):
                nonlocal arrived
                arrived += 1
                if arrived == 3:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 10)
                return await _consume(*args, **kwargs)
            repository.consume_order_approval = together
            plans.append((engine, decision, check))
        results = await asyncio.wait_for(asyncio.gather(*(e.create_order(d, c, current_price=100)
            for e, d, c in plans), return_exceptions=True), 20)
        accepted = [r for r in results if isinstance(r, PaperOrder)]
        rejected = [r for r in results if isinstance(r, Exception)]
        assert arrived == 3 and len(accepted) == 1 and len(rejected) == 2
        assert all(isinstance(r, DatabaseError) and "stale for the durable portfolio" in str(r) for r in rejected)
        positions = await Repository(databases[0]).load_open_position_exposures()
        assert len(positions) == 1 and sum(p.notional for p in positions) == 4000
        assert len(await Repository(databases[0]).list_oms_orders()) == 1
        # Fresh instance, independent connection, same durable portfolio.
        restored = RiskManager(limits, state, AuditService(repository=Repository(databases[1])),
                              repository=Repository(databases[1]))
        await restored.initialize()
        assert len(restored.position_exposures()) == 1
        assert restored.position_exposures()[0].notional == 4000
        return {"opposing": opposing, "distinct_approvals": 3, "independent_pools": 3,
                "accepted": 1, "stale_rejected": 2, "gross": 4000, "restored_positions": 1,
                "policy": "first durable admission wins; stale approvals require fresh risk, never automatic retry"}
    finally:
        for database in databases:
            await database.dispose()


async def validate_locked_expiry(settings):
    import asyncpg
    from datetime import timedelta
    from app.audit.service import AuditService
    from app.core.errors import DataQualityError
    from app.core.state_machine import SystemState, SystemStateMachine
    from app.database.repositories.repository import Repository
    import app.database.repositories.repository as repository_module
    from app.database.session import Database
    from app.paper_trading.engine import PaperTradingEngine
    from app.risk.manager import RiskManager
    from app.schemas.common import utcnow
    from app.schemas.risk import RiskLimits
    database = Database(settings.database_url, pool_size=1, max_overflow=0)
    repository = Repository(database)
    holder = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://"))
    transaction = holder.transaction()
    pending = None
    try:
        state = SystemStateMachine()
        await state.transition(SystemState.INITIALIZING, reason="isolated probe", actor="validation")
        await state.transition(SystemState.PAPER, reason="isolated probe", actor="validation")
        audit = AuditService(repository=repository)
        manager = RiskManager(RiskLimits(), state, audit, repository=repository)
        await manager.initialize()
        engine = PaperTradingEngine(audit, manager, repository=repository)
        decision = Decision(correlation_id=str(uuid4()), symbol="BTCUSDT", timeframe="15m",
                            strategy="SCALP_15M_v1", candidate_action="BUY", confidence=80)
        await repository.save_decision(decision)
        deadline = utcnow() + timedelta(seconds=30)
        check = await manager.check(decision, entry_price=100, atr=1, signal_expires_at=deadline)
        assert check.approved
        await transaction.start()
        await holder.fetchval("select singleton_id from capital_cipher.risk_control_state where singleton_id=1 for update")
        pending = asyncio.create_task(engine.create_order(decision, check, current_price=100))
        timeout = asyncio.get_running_loop().time() + 10
        while not await holder.fetchval("select count(*) from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid() and wait_event_type='Lock' and query like '%risk_control_state%'"):
            assert not pending.done(), "PHASE_C_REAL_ROW_LOCK_WAIT_REQUIRED"
            assert asyncio.get_running_loop().time() < timeout, "PHASE_C_ROW_LOCK_WAIT_TIMEOUT"
            await asyncio.sleep(.01)
        with patch.object(repository_module, "_now", lambda: deadline + timedelta(seconds=1)):
            await transaction.commit()
            try:
                await asyncio.wait_for(pending, 10)
            except DataQualityError:
                pass
            else:
                raise AssertionError("PHASE_C_EXPIRED_ADMISSION_WAS_NOT_REJECTED")
        assert not await repository.load_paper_orders()
        _, approval = await repository.load_central_risk_evaluation_by_key(decision.decision_id)
        assert approval.status.value == "ACTIVE"
        return {"row_lock_wait_observed": True, "controlled_clock": True,
                "expired_rejected": True, "orders": 0, "approval_not_consumed": True}
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await holder.close()
        await database.dispose()


def crash_child(config, boundary, marker_path):
    # Spawn has no inherited pool. os._exit deliberately skips ALL Python cleanup.
    async def run():
        settings = Settings(_env_file=None, **config)
        context = build_context(settings, with_database=True)
        app = create_app(context, with_market_data=False)
        marker = Path(marker_path)
        arrivals = 0
        def die():
            waiters = len(context.orchestrator._cycle_lock._waiters or ())
            assert arrivals == 3 and waiters == 2, "PHASE_C_QUEUED_DESKS_REQUIRED"
            with marker.open("w", encoding="utf-8") as out:
                json.dump({"boundary": boundary, "reached": True,
                           "concurrent_arrivals": arrivals, "queued_desks": waiters}, out)
                out.flush()
                os.fsync(out.fileno())
            os._exit(77)
        with no_exchange_http():
            async with app.router.lifespan_context(app):
                closes = await prepared_closes(context)
                # Private parent/child handoff. No configuration or credentials.
                marker.with_suffix(".closes.json").write_text(json.dumps([c.model_dump(mode="json") for c in closes]))
                original = context.repository.consume_order_approval
                flush = AsyncSession.flush
                async def crashing_flush(session, *args, **kwargs):
                    paper_insert = any(isinstance(row, PaperOrderModel) for row in session.new)
                    result = await flush(session, *args, **kwargs)
                    if paper_insert:
                        die()  # Both insert statements have run; COMMIT has not.
                    return result
                async def consume(*args, **kwargs):
                    if boundary == "before_consume":
                        die()
                    if boundary == "uncommitted_flush":
                        with patch.object(AsyncSession, "flush", crashing_flush):
                            return await original(*args, **kwargs)
                    result = await original(*args, **kwargs)
                    if boundary == "committed_unacknowledged":
                        die()  # Durable book exists; engine has not registered it.
                    return result
                create = context.paper_engine.create_order
                async def after_book(*args, **kwargs):
                    result = await create(*args, **kwargs)
                    die()  # Engine registered; OMS caller has not returned.
                    return result
                async def arrive(candle):
                    nonlocal arrivals
                    arrivals += 1
                    return await context.orchestrator.on_candle_closed(candle)
                with patch.object(context.repository, "consume_order_approval", consume):
                    if boundary == "book_registered":
                        with patch.object(context.paper_engine, "create_order", after_book):
                            await asyncio.gather(*(arrive(c) for c in closes))
                    else:
                        await asyncio.gather(*(arrive(c) for c in closes))
                raise RuntimeError("PHASE_C_CRASH_BOUNDARY_NOT_REACHED")
    asyncio.run(run())


async def validate_crash_recovery(settings, boundary, directory):
    if boundary not in CRASH_BOUNDARIES:
        raise ValueError("PHASE_C_CRASH_BOUNDARY_REFUSED")
    marker = Path(directory) / (boundary + "-" + uuid4().hex + ".json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    process = multiprocessing.get_context("spawn").Process(
        target=crash_child, args=(settings.model_dump(mode="json"), boundary, str(marker)))
    process.start()
    try:
        deadline = asyncio.get_running_loop().time() + 60
        while process.is_alive():
            assert asyncio.get_running_loop().time() < deadline, "PHASE_C_CRASH_CHILD_TIMEOUT"
            await asyncio.sleep(.05)
        process.join()
        assert process.exitcode == 77, "PHASE_C_CRASH_CHILD_DID_NOT_REACH_BOUNDARY"
        assert json.loads(marker.read_text()) == {"boundary": boundary, "reached": True,
                                               "concurrent_arrivals": 3, "queued_desks": 2}
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
            if process.is_alive():
                process.kill()
                await asyncio.to_thread(process.join, 5)
        process.close()
    committed = boundary in {"committed_unacknowledged", "book_registered"}
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        orders = await restored.repository.load_paper_orders()
        assert len(orders) == int(committed)
        assert len(restored.paper_engine.open_orders) == restored.risk_manager.state.open_positions == int(committed)
        assert restored.paper_engine.balance == 10_000
        assert restored.orchestrator.status()["paper_execution_timing"]["receipt_to_fill_ms"]["samples"] == 0
        # Recover original decision/check; no new decision identity or renewed TTL.
        from sqlalchemy import select
        from app.database.models import OrderApprovalModel
        from app.api.routes.decisions import durable_decision
        async with restored.repository._db.session() as session:
            approval = (await session.scalars(select(OrderApprovalModel))).one()
            approval_status = approval.status
            decision_id = approval.decision_id
        assert approval_status == ("CONSUMED" if committed else "ACTIVE")
        check, approval = await restored.repository.load_central_risk_evaluation_by_key(decision_id)
        decision = Decision.model_validate(durable_decision(await restored.repository.get_decision(decision_id)))
        if committed:
            mirrored = await restored.oms_service.submit_approved(decision, check, current_price=orders[0].entry_price)
            assert mirrored.oms_order_id == orders[0].paper_order_id
            assert len(await restored.repository.load_paper_orders()) == 1
        # All three arrivals were submitted concurrently before the process died.
        # Replay only the persisted first candle: incomplete decision execution
        # is deliberately NOT retried. Missed signals are possible, never a new fill.
        closes = [Candle.model_validate(c) for c in json.loads(marker.with_suffix(".closes.json").read_text())]
        trust_fixture_clock(restored)
        assert await restored.orchestrator.on_candle_closed(closes[0]) is None
        assert len(await restored.repository.load_paper_orders()) == int(committed)
        assert len(await restored.repository.list_oms_orders()) == int(committed)
        exposures = restored.risk_manager.position_exposures()
        assert len(exposures) == int(committed)
    return {"boundary": boundary, "process_exit": 77, "concurrent_arrivals": 3,
            "queued_desks": 2,
            "orders_after_rebuild": int(committed), "approval_status": approval_status,
            "rollback_or_reconciliation_verified": True, "replay_no_duplicate": True,
            "recovery_policy": "restore committed book; do not retry an incomplete candle as a new execution",
            "scope": "real process exit and SQL transaction boundary; not host or database-server crash"}

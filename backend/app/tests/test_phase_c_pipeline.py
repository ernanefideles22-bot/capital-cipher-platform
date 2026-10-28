"""Real PAPER critical pipeline: three desks, durable book, replay and expiry."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.errors import DataQualityError, ValidationError
from app.main import create_app
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.schemas.common import utcnow
from app.tests.conftest import make_decision
from app.tests.test_month_6_central_risk import risk_stack
from app.tests.test_office_async import candles, seed, settings
from app.validation.paper_flow import fresh_fixture_close, trust_fixture_clock


async def exercise_three_desks(config):
    context = build_context(config, with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert context.oms_service.target_environment.value == "PAPER"
        assert len(context.agent_registry.registrations()) == 300
        streams = [candles(symbol, timeframe=tf) for symbol, tf in (
            ("BTCUSDT", "15m"), ("ETHUSDT", "1h"), ("SOLUSDT", "4h"))]
        for items in streams: await seed(context, items)
        closes = [fresh_fixture_close(items[-1], items[-2]) for items in streams]
        trust_fixture_clock(context)
        decisions = await asyncio.gather(*(context.orchestrator.on_candle_closed(c) for c in closes))
        assert all(d is not None and d.risk_status.value in {"APPROVED", "REDUCED"} for d in decisions)
        assert {d.strategy for d in decisions} == {"SCALP_15M_v1", "DAY_1H_v1", "SWING_4H_v1"}
        orders = list(context.paper_engine.open_orders.values())
        assert len(orders) == context.risk_manager.state.open_positions == 3
        assert all(order.position_size <= 2000 for order in orders)
        assert sum(order.position_size for order in orders) <= 6000
        for decision, candle in zip(decisions, closes):
            check, approval = await context.repository.load_central_risk_evaluation_by_key(decision.decision_id)
            expiry = approval.expires_at
            if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
            assert expiry <= candle.closed_at + timedelta(milliseconds=config.max_market_data_delay_ms)
            assert check.effective_limits["signal_expires_at_unix"] == pytest.approx(expiry.timestamp(), abs=1e-6, rel=0)
        metrics = context.orchestrator.status()["paper_execution_timing"]
        assert metrics["receipt_to_fill_ms"]["samples"] == 3
        assert all(v["samples"] == 1 for v in metrics["by_timeframe"].values())
        assert len({row["correlation_id"] for row in metrics["recent"]}) == 3
        assert {row["paper_order_id"] for row in metrics["recent"]} == {order.paper_order_id for order in orders}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for order in orders:
                payload = (await client.get(f"/api/v1/paper/orders/{order.paper_order_id}")).json()["data"]["order"]
                assert payload == order.model_dump(mode="json")
                chain = (await client.get(f"/api/v1/audit/correlation/{order.correlation_id}")).json()["data"]["chain"]
                assert {"DECISION_CANDIDATE", "RISK_CHECK", "PAPER_ORDER_CREATED"} <= {row["audit_type"] for row in chain}
            api_timing = (await client.get("/api/v1/orchestrator/status")).json()["data"]["paper_execution_timing"]
            assert api_timing == metrics
        assert all(value is None for value in await asyncio.gather(*(context.orchestrator.on_candle_closed(c) for c in closes)))
        assert len(context.paper_engine.open_orders) == 3
        assert context.orchestrator.status()["paper_execution_timing"]["receipt_to_fill_ms"]["samples"] == 3
        assert context.orchestrator.status()["paper_execution_timing"]["attempts"] == 6
    restored = build_context(config, with_database=True)
    restored_app = create_app(restored, with_market_data=False)
    async with restored_app.router.lifespan_context(restored_app):
        assert len(restored.paper_engine.open_orders) == restored.risk_manager.state.open_positions == 3
        assert {o.paper_order_id for o in orders} == set(restored.paper_engine.open_orders)
        assert restored.paper_engine.balance == 10_000
        # Samples are explicitly volatile, not replayed as new fills at boot.
        assert restored.orchestrator.status()["paper_execution_timing"]["receipt_to_fill_ms"]["p95"] is None


async def test_three_desks_complete_pipeline_replay_and_restore(tmp_path):
    await exercise_three_desks(settings(tmp_path, max_single_position_percent=20, max_strategy_exposure_percent=20))


class ControlledClock(datetime):
    offset = timedelta(0)
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + cls.offset


async def test_signal_expiring_while_queued_has_no_fill_sample(tmp_path, monkeypatch):
    import app.market_data.data_quality as quality
    import app.orchestrator.service as orchestrator_module
    context = build_context(settings(tmp_path), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles();await seed(context, items)
        monkeypatch.setattr(ControlledClock, "offset", timedelta(0))
        monkeypatch.setattr(quality, "datetime", ControlledClock)
        monkeypatch.setattr(orchestrator_module, "datetime", ControlledClock)
        async with context.orchestrator._cycle_lock:
            task = asyncio.create_task(context.orchestrator.on_candle_closed(items[-1]))
            await asyncio.sleep(0)
            monkeypatch.setattr(ControlledClock, "offset", timedelta(seconds=10))
        assert await task is None
        assert not context.paper_engine.open_orders
        metrics = context.orchestrator.status()["paper_execution_timing"]
        assert metrics["receipt_to_fill_ms"]["p95"] is None
        assert metrics["recent"][-1]["error_code"] == "DATA_QUALITY_ERROR"


async def test_signal_expiring_during_oms_lookup_cannot_fill(tmp_path, monkeypatch):
    import app.risk.manager as risk_module
    context = build_context(settings(tmp_path), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles();await seed(context, items)
        lookup = context.oms_service.get_order
        monkeypatch.setattr(ControlledClock, "offset", timedelta(0))
        monkeypatch.setattr(risk_module, "datetime", ControlledClock)
        async def delayed_lookup(*args, **kwargs):
            result = await lookup(*args, **kwargs)
            monkeypatch.setattr(ControlledClock, "offset", timedelta(seconds=10))
            return result
        monkeypatch.setattr(context.oms_service, "get_order", delayed_lookup)
        assert await context.orchestrator.on_candle_closed(items[-1]) is None
        assert not context.paper_engine.open_orders
        assert not await context.repository.load_paper_orders()
        row = context.orchestrator.status()["paper_execution_timing"]["recent"][-1]
        assert row["error_code"] == "DATA_QUALITY_ERROR"
        assert "submit_started" in row["milestones_ms"]
        assert "paper_fill_committed" not in row["milestones_ms"]


async def test_signal_deadline_is_immutable_and_never_extends_approval_ttl():
    _, _, manager, _ = await risk_stack()
    decision = make_decision()
    deadline = utcnow() + timedelta(seconds=5)
    check = await manager.check(decision, entry_price=100, atr=1, signal_expires_at=deadline)
    assert manager._approvals[check.approval_id].expires_at == deadline
    with pytest.raises(ValidationError, match="different input"):
        await manager.check(decision, entry_price=100, atr=1, signal_expires_at=deadline + timedelta(seconds=5))
    other = await manager.check(make_decision(), entry_price=100, atr=1, signal_expires_at=utcnow()+timedelta(hours=1))
    approval = manager._approvals[other.approval_id]
    assert approval.expires_at - approval.created_at == timedelta(seconds=manager.limits.approval_ttl_seconds)
    with pytest.raises(DataQualityError, match="expired"):
        await manager.check(make_decision(), entry_price=100, atr=1, signal_expires_at=utcnow()-timedelta(seconds=1))
    with pytest.raises(ValidationError, match="timezone"):
        await manager.check(make_decision(), entry_price=100, atr=1, signal_expires_at=datetime.now())


async def test_cancellation_while_waiting_releases_timing_context(tmp_path):
    context = build_context(settings(tmp_path), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles();await seed(context, items)
        async with context.orchestrator._cycle_lock:
            task = asyncio.create_task(context.orchestrator.on_candle_closed(items[-1]))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        metrics = context.orchestrator.status()["paper_execution_timing"]
        assert metrics["recent"][-1]["outcome"] == "CANCELLED"
        assert metrics["receipt_to_fill_ms"]["samples"] == 0
        assert not context.paper_engine.open_orders


async def test_publication_failure_after_commit_does_not_hide_the_fill(tmp_path, monkeypatch):
    from app.core.errors import DatabaseError
    from app.schemas.events import EventTypes
    context = build_context(settings(tmp_path), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles();await seed(context, items)
        publish = context.event_bus.publish
        async def fail_after_fill(topic, event_type, payload, **kwargs):
            if event_type == EventTypes.OMS_ORDER_ACCEPTED:
                raise DatabaseError("Injected post-fill event failure")
            return await publish(topic, event_type, payload, **kwargs)
        monkeypatch.setattr(context.event_bus, "publish", fail_after_fill)
        assert await context.orchestrator.on_candle_closed(items[-1]) is None
        assert len(await context.repository.load_paper_orders()) == 1
        metrics = context.orchestrator.status()["paper_execution_timing"]
        assert metrics["recent"][-1]["outcome"] == "FILLED"
        assert metrics["recent"][-1]["error_code"] == "DATABASE_ERROR"
        assert metrics["receipt_to_fill_ms"]["samples"] == 1


async def new_pg17_database():
    """Migration-owned fixture; retained until the disposable CI service ends."""
    import asyncpg
    url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    assert url.hostname in {"localhost", "127.0.0.1", "::1"}, "Refuse hosted databases"
    name = "capital_cipher_phase_c_" + uuid4().hex[:12]
    connection = await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await connection.fetchval("show server_version_num")) // 10000 == 17
        await connection.execute(f'create database "{name}" template template0')
    finally:
        await connection.close()
    target = urlunsplit(url._replace(path="/"+name))
    connection = await asyncpg.connect(target)
    try:
        for migration in sorted((Path(__file__).resolve().parents[3]/"supabase/migrations").glob("*.sql")):
            await connection.execute(migration.read_text())
    finally:
        await connection.close()
    return target


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_pg17_redis_three_desks_pipeline_replay_and_restore(tmp_path):
    target = await new_pg17_database()
    redis = os.environ.get("REDIS_TEST_URL")
    if redis:
        assert urlsplit(redis).hostname in {"localhost", "127.0.0.1", "::1"}
    await exercise_three_desks(settings(tmp_path,
        app_env="local",
        database_url=target.replace("postgresql://", "postgresql+asyncpg://"),
        redis_url=redis, event_broker_required=bool(redis), redis_stream_prefix="cc-phase-c-"+uuid4().hex[:12],
        max_single_position_percent=20, max_strategy_exposure_percent=20))


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_pg17_signal_expiry_is_rechecked_after_real_row_lock_wait(monkeypatch):
    import asyncpg
    import app.database.repositories.repository as repository_module
    target = await new_pg17_database()
    database = Database(target.replace("postgresql://", "postgresql+asyncpg://"), pool_size=1)
    repository = Repository(database)
    holder = await asyncpg.connect(target)
    pending = None
    transaction = holder.transaction()
    try:
        _, _, manager, engine = await risk_stack(repository=repository)
        await manager.initialize()
        decision = make_decision();await repository.save_decision(decision)
        deadline = utcnow()+timedelta(seconds=30)
        check = await manager.check(decision, entry_price=100, atr=1, signal_expires_at=deadline)
        assert check.approved
        await transaction.start()
        await holder.fetchval("select singleton_id from capital_cipher.risk_control_state where singleton_id=1 for update")
        pending = asyncio.create_task(engine.create_order(decision, check, current_price=100))
        timeout = asyncio.get_running_loop().time()+10
        while True:
            waiting = await holder.fetchval("select count(*) from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid() and wait_event_type='Lock' and query like '%risk_control_state%'")
            if waiting: break
            assert not pending.done(), "Submission must actually wait for the portfolio row lock"
            assert asyncio.get_running_loop().time() < timeout
            await asyncio.sleep(.01)
        # Advance the gate's wall clock only after observing a real PostgreSQL
        # lock wait. This tests expiry without a slow/flaky 30-second sleep.
        monkeypatch.setattr(repository_module, "_now", lambda: deadline+timedelta(seconds=1))
        await transaction.commit()
        with pytest.raises(DataQualityError, match="expired before durable admission"):
            await asyncio.wait_for(pending, 10)
        assert not engine.open_orders
        assert not await repository.load_paper_orders()
        _, approval = await repository.load_central_risk_evaluation_by_key(decision.decision_id)
        assert approval.status.value == "ACTIVE", "Rejected admission must not consume its approval"
    finally:
        if pending is not None and not pending.done():
            pending.cancel();await asyncio.gather(pending, return_exceptions=True)
        await holder.close()
        await database.dispose()

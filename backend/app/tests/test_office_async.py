"""Two-speed PAPER invariants, including a genuinely non-cooperative CPU loop."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from math import exp, sin

import pytest
from pydantic import ValidationError

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import DatabaseError
from app.core.state_machine import SystemState
from app.main import create_app
from app.market_data.runtime import runtime_market_subscriptions
from app.office.isolation import IsolatedAgentRunner
from app.office.scheduler import ShadowOffice
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import AgentStatus, Exchange, Signal, utcnow
from app.schemas.market import Candle
from app.strategy.engine import StrategyEngine
from app.validation.paper_flow import trust_fixture_clock


class HungShadow(BaseAgent):
    name = "MomentumAgent"
    version = "2.0.0"
    capabilities = ("test-cpu-isolation",)
    max_attempts = 1

    async def _analyze(self, agent_input):
        if getattr(self, "started_path", None):
            from pathlib import Path
            Path(self.started_path).write_text("CPU loop started")
        while True:
            pass


class FastShadow(BaseAgent):
    name = "FastShadowAgent"
    capabilities = ("test-isolation",)

    async def _analyze(self, agent_input):
        return self._output(AgentStatus.COMPLETED, Signal.HOLD, 50, "Fast diagnostic")


def request(agent, suffix="1", *, deadline=None):
    return AgentExecutionRequest(input=AgentInput(agent_name=agent.name,
        request_id=f"request-{agent.name}-{suffix}", correlation_id=f"correlation-{suffix}",
        symbol="BTCUSDT", timeframe="15m",
        config={"office_deadline_at": deadline.isoformat()} if deadline else {}),
        agent_version=agent.version)


def candles(symbol="BTCUSDT", direction=1):
    now = utcnow()
    result = []
    for i in range(220):
        close = 100 * exp(direction * .002 * i) * (1 + direction * .01 * sin(1.1 * i))
        result.append(Candle(exchange=Exchange.BINANCE, symbol=symbol, timeframe="15m",
            open=close * .999, high=close * 1.003, low=close * .997, close=close,
            volume=100, closed_at=now - timedelta(minutes=15 * (219 - i))))
    return result


def settings(tmp_path, **overrides):
    values = dict(office_async_enabled=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'office.db'}",
        redis_url=None, enable_market_data=False, operations_monitor_enabled=False,
        backfill_worker_enabled=False, data_lake_root=str(tmp_path / "lake"), log_level="ERROR")
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def seed(context, items):
    for item in items[:-1]:
        context.candle_store.add(item)
    if context.repository:
        await context.repository.save_candles(items[:-1])
    context.market_connected = True
    trust_fixture_clock(context)


async def test_noncooperative_cpu_agent_is_killed_while_event_loop_remains_responsive(tmp_path):
    runner = IsolatedAgentRunner(concurrency=1, timeout_seconds=3, breaker_failures=1, cooldown_seconds=.05)
    agent = HungShadow()
    agent.started_path = str(tmp_path / "started")
    task = asyncio.create_task(runner.run(agent, request(agent).input))
    ticks = 0
    while not task.done():
        await asyncio.sleep(.01)
        ticks += 1
    result = await task
    assert ticks >= 10
    assert result.status == AgentStatus.TIMEOUT
    assert not runner._children
    assert runner.timeouts == 1
    assert (tmp_path / "started").exists(), "The child must actually enter its CPU loop"
    blocked = await runner.run(agent, request(agent, "2").input)
    assert "OFFICE_CIRCUIT_OPEN" in blocked.warnings
    await asyncio.sleep(.06)
    healthy = FastShadow()
    runner._timeout = 5
    assert (await runner.run(healthy, request(healthy).input)).status == AgentStatus.COMPLETED
    await runner.aclose()
    assert not runner._children


async def test_real_paper_order_does_not_wait_for_shadow_and_survives_restart(tmp_path):
    config = settings(tmp_path)
    context = build_context(config, with_database=True)
    hung_agent = HungShadow()
    hung_agent.started_path = str(tmp_path / "concurrent-started")
    context.agent_registry.replace(hung_agent, expected_version="1.0.0")
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles()
        await seed(context, items)
        started = time.monotonic()
        decision = await context.orchestrator.on_candle_closed(items[-1])
        duration = time.monotonic() - started
        assert decision is not None
        assert decision.risk_status.value in {"APPROVED", "REDUCED"}
        assert len(decision.agent_summary) == 3
        assert duration < config.office_primary_timeout_seconds
        assert len(context.paper_engine.open_orders) == 1
        opened, = context.paper_engine.open_orders.values()
        assert opened.approval_id and opened.risk_check_id
        assert await context.orchestrator.on_candle_closed(items[-1]) is None
        assert len(context.paper_engine.open_orders) == 1
        # Start a real CPU-hung diagnostic while another live PAPER decision runs.
        runtime = context.agent_runtime
        hung = context.agent_registry.get("MomentumAgent")
        job = asyncio.create_task(runtime.execute(request(hung, "concurrent")))
        deadline = time.monotonic() + 4
        while not (tmp_path / "concurrent-started").exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        other = candles("ETHUSDT")
        await seed(context, other)
        second = await asyncio.wait_for(context.orchestrator.on_candle_closed(other[-1]), 3)
        assert second is not None and len(second.agent_summary) == 3
        assert not job.done(), "CPU-hung diagnostic should still be isolated while trading finishes"
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
    restored = build_context(config, with_database=True)
    restored_app = create_app(restored, with_market_data=False)
    async with restored_app.router.lifespan_context(restored_app):
        assert restored.paper_engine.open_orders[opened.paper_order_id].model_dump() == opened.model_dump()
        assert restored.risk_manager.state.open_positions == len(restored.paper_engine.open_orders)


@pytest.mark.parametrize("fault", ["database", "broker", "risk", "audit", "market", "expired", "primary_timeout"])
async def test_fast_lane_fails_closed_on_critical_faults(tmp_path, monkeypatch, fault):
    config = settings(tmp_path, office_primary_timeout_seconds=.03 if fault == "primary_timeout" else 3)
    context = build_context(config, with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles()
        await seed(context, items)
        async def unavailable(*args, **kwargs):
            raise DatabaseError("Injected unavailable dependency")
        if fault == "database":
            monkeypatch.setattr(context.repository, "save_candle", unavailable)
        elif fault == "broker":
            monkeypatch.setattr(context.event_bus, "publish", unavailable)
        elif fault == "risk":
            monkeypatch.setattr(context.risk_manager, "check", unavailable)
        elif fault == "audit":
            context.audit_service.fail_mode = True
        elif fault == "market":
            context.market_connected = False
        elif fault == "expired":
            items[-1] = items[-1].model_copy(update={"closed_at": utcnow() - timedelta(seconds=10)})
        else:
            async def slow(_):
                await asyncio.sleep(1)
            monkeypatch.setattr(context.agent_runtime, "execute_primary", slow)
        await context.orchestrator.on_candle_closed(items[-1])
        assert not context.paper_engine.open_orders


async def test_primary_queue_priority_and_shadow_deadline():
    shadow = FastShadow()
    primary = FastShadow()
    primary.name, primary.decision_role = "PrimaryAgent", "PRIMARY"
    runtime = AgentRuntime(AgentRegistry([shadow, primary]))
    old = await runtime.submit(request(shadow))
    important = await runtime.submit(request(primary))
    jobs = await runtime.repository.claim_next_agent_executions(worker_id="worker", lease_seconds=30, limit=1)
    assert jobs[0].execution_id == important.execution_id
    runtime.shadow_runner = IsolatedAgentRunner()
    expired_request = request(shadow, "stale", deadline=utcnow() - timedelta(seconds=1))
    expired_request.input.timestamp = utcnow() - timedelta(seconds=2)
    stale = await runtime.execute(expired_request)
    assert stale.job.output.status == AgentStatus.SKIPPED
    assert "OFFICE_ANALYSIS_EXPIRED" in stale.job.output.warnings
    with pytest.raises(Exception, match="only accepts PRIMARY"):
        await runtime.execute_primary([request(shadow)])
    await runtime.shadow_runner.aclose()


async def test_admission_queue_is_bounded_deduplicated_prioritized_and_expiring():
    class FakeRuntime:
        registry = AgentRegistry()
        async def execute_many(self, requests): return []
    office = ShadowOffice(FakeRuntime(), capacity=2, ttl_seconds=1)
    slow = candles()[-1].model_copy(update={"timeframe": "4h"})
    fast = candles()[-1]
    assert office.offer(slow)
    assert not office.offer(slow)
    assert office.offer(fast)
    assert not office.offer(candles("ETHUSDT")[-1])
    assert office.status()["rejected"] == 1
    assert office._queue._queue[0][3].timeframe == "15m"
    await office.run_once()
    await asyncio.sleep(1.01)
    await office.run_once()
    assert office.completed == 1 and office.expired == 1
    assert not office._pending


def test_market_streams_match_existing_strategies_and_keep_legacy_switch():
    engine = StrategyEngine()
    legacy = Settings(_env_file=None, default_timeframe="5m")
    assert {tf for _, tf in runtime_market_subscriptions(legacy, engine)} == {"5m"}
    config = Settings(_env_file=None, office_async_enabled=True, default_timeframe="5m")
    streams = runtime_market_subscriptions(config, engine)
    assert len(streams) == 9
    assert {tf for _, tf in streams} == {"15m", "1h", "4h"}
    assert all(engine.select(symbol, tf) for symbol, tf in streams)
    engine.set_enabled("DAY_1H_v1", False)
    assert all(tf != "1h" for _, tf in runtime_market_subscriptions(config, engine))


@pytest.mark.parametrize("change", [{"oms_execution_environment": "TESTNET"}, {"oms_testnet_enabled": True}])
def test_office_refuses_testnet(change):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, office_async_enabled=True, **change)


async def test_concurrent_redelivery_and_symbols_share_one_risk_portfolio(tmp_path):
    context = build_context(settings(tmp_path, max_open_positions=1), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        btc, eth = candles(), candles("ETHUSDT")
        await seed(context, btc)
        await seed(context, eth)
        results = await asyncio.gather(*[
            context.orchestrator.on_candle_closed(btc[-1] if i % 2 == 0 else eth[-1])
            for i in range(16)])
        assert sum(result is not None for result in results) == 2
        assert len(context.paper_engine.open_orders) == 1
        assert context.risk_manager.state.open_positions == 1
        assert context.orchestrator.status()["cycle_latency_ms"]["samples"] == 16


async def test_confirmation_veto_is_not_silently_removed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    context = build_context(settings(tmp_path), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles()
        await seed(context, items)
        monkeypatch.setattr(context.weighted_consensus_service._experiments, "active",
                            lambda: SimpleNamespace(mode="CONFIRMATION"))
        decision = await context.orchestrator.on_candle_closed(items[-1])
        assert decision.candidate_action.value == "WAIT"
        assert "OFFICE_CONFIRMATION_NOT_READY" in decision.warnings
        assert not context.paper_engine.open_orders


async def test_signal_is_rechecked_after_slow_risk_before_oms(tmp_path, monkeypatch):
    context = build_context(settings(tmp_path, max_market_data_delay_ms=1500), with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        items = candles()
        await seed(context, items)
        original = context.risk_manager.check
        invoked = []
        async def slow(*args, **kwargs):
            invoked.append(True)
            result = await original(*args, **kwargs)
            await asyncio.sleep(1.6)
            return result
        monkeypatch.setattr(context.risk_manager, "check", slow)
        assert await context.orchestrator.on_candle_closed(items[-1]) is None
        assert invoked
        assert not context.paper_engine.open_orders


@pytest.mark.parametrize("value", ["invalid", "2026-10-24T10:00:00", 123])
async def test_malformed_deadline_cannot_poison_worker(value):
    agent = FastShadow()
    runtime = AgentRuntime(AgentRegistry([agent]))
    runtime.shadow_runner = IsolatedAgentRunner()
    req = request(agent)
    req.input.config["office_deadline_at"] = value
    with pytest.raises(Exception, match="Invalid analytical deadline"):
        await runtime.submit(req)
    assert not await runtime.list_jobs()
    await runtime.shadow_runner.aclose()


async def test_snapshot_does_not_include_future_candles():
    from app.agents.quant import QuantAgent
    from app.market_data.store import CandleStore
    store = CandleStore()
    now = utcnow()
    first = candles()[-1].model_copy(update={"closed_at": now})
    future = first.model_copy(update={"closed_at": now + timedelta(minutes=15)})
    store.add(first)
    store.add(future)
    agent = QuantAgent(store)
    runner = IsolatedAgentRunner()
    req = request(agent)
    req.input.timestamp = now
    clone = await runner._snapshot(agent, req.input)
    assert clone._store.get("BINANCE", "BTCUSDT", "15m") == [first]
    assert store.get("BINANCE", "BTCUSDT", "15m") == [first, future]
    await runner.aclose()


async def test_recovery_replays_only_analysis_with_durable_identity():
    from app.office.scheduler import shadow_correlation
    class FakeRuntime:
        registry = AgentRegistry()
        async def execute_many(self, requests): return []
    item = candles()[-1]
    class FakeRepository:
        async def list_candles(self, **kwargs): return [item]
    office = ShadowOffice(FakeRuntime())
    await office.recover(FakeRepository(), [(item.symbol, item.timeframe)])
    await office.recover(FakeRepository(), [(item.symbol, item.timeframe)])
    assert office.accepted == 1
    assert shadow_correlation(item) == shadow_correlation(item.model_copy())
    await office.run_once()
    assert office.completed == 1


@pytest.mark.parametrize("change", [{"agent_worker_enabled": False}, {"agent_lease_seconds": 5},
    {"database_url": "postgresql+asyncpg://unused.invalid/test", "database_pool_size": 2}])
def test_office_configuration_preserves_queue_and_database_capacity(change):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, office_async_enabled=True, **change)

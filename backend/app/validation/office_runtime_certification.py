"""Bounded, synthetic PAPER probes; never imported by the production runtime.

The hosted entrypoint must select new isolated databases before calling these
functions. Critical deadlines and the real risk/OMS path remain unchanged.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from math import exp, sin
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

from app.agents.base import BaseAgent
from app.api.context import build_context
from app.core.errors import AuditError, DatabaseError
from app.main import create_app
from app.schemas.agents import AgentInput
from app.schemas.common import AgentStatus, Exchange
from app.schemas.market import Candle
from app.validation.paper_flow import fixture_interval, fresh_fixture_close, trust_fixture_clock


class CpuBoundDiagnostic(BaseAgent):
    """A real non-cooperative child, with no repository or order authority."""
    name = "MomentumAgent"
    version = "2.0.0"
    capabilities = ("certification-cpu-isolation",)
    max_attempts = 1

    async def _analyze(self, agent_input):
        import os
        Path(self.started_path).write_text(str(os.getpid()), encoding="utf-8")
        while True:
            pass


def probe_candles(timeframe):
    interval = fixture_interval(timeframe)
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(220):
        close = 100 * exp(.002 * index) * (1 + .01 * sin(1.1 * index))
        rows.append(Candle(exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe=timeframe,
            open=close * .999, high=close * 1.003, low=close * .997,
            close=close, volume=100, closed_at=now - interval * (219 - index)))
    return rows


async def seed_probe(context, rows):
    for candle in rows[:-1]:
        context.candle_store.add(candle)
    await context.repository.save_candles(rows[:-1])
    context.market_connected = True
    trust_fixture_clock(context)


async def certify_cpu_isolation(settings, timeframe, marker_directory):
    """Run a PAPER decision while a proven CPU loop occupies a SHADOW child."""
    context = build_context(settings, with_database=True)
    marker = Path(marker_directory) / (timeframe + "-" + uuid4().hex)
    marker.parent.mkdir(parents=True, exist_ok=True)
    hung = CpuBoundDiagnostic()
    hung.started_path = str(marker)
    # The injected supervisor child has no durable request or cache authority.
    # Keep all 300 registered agents and their governed affiliations intact.
    assert len(context.agent_registry.registrations()) == 300
    assert context.agent_registry.get("MomentumAgent").version == "1.0.0"
    app = create_app(context, with_market_data=False)
    task = None
    async with app.router.lifespan_context(app):
        assert not context.paper_engine.open_orders
        rows = probe_candles(timeframe)
        await seed_probe(context, rows)
        agent_input = AgentInput(agent_name=hung.name, correlation_id=str(uuid4()),
            symbol="BTCUSDT", timeframe=timeframe)
        # Use the runtime's actual SHADOW supervisor directly. A public execute()
        # submission may return a lease claimed concurrently by the worker and
        # would not prove that this diagnostic had entered its CPU loop.
        task = asyncio.create_task(context.agent_runtime.shadow_runner.run(hung, agent_input))
        try:
            deadline = time.monotonic() + 4
            while not marker.exists():
                if task.done() or time.monotonic() >= deadline:
                    raise RuntimeError("CERTIFICATION_CPU_LOOP_NOT_STARTED")
                await asyncio.sleep(.01)
            child_pid = int(marker.read_text(encoding="utf-8"))
            rows[-1] = fresh_fixture_close(rows[-1], rows[-2])
            trust_fixture_clock(context)
            started = time.monotonic()
            decision = await asyncio.wait_for(
                context.orchestrator.on_candle_closed(rows[-1]),
                settings.office_primary_timeout_seconds,
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            assert decision is not None and len(decision.agent_summary) == 3
            assert decision.risk_status.value in {"APPROVED", "REDUCED"}
            assert not task.done(), "PAPER must finish while the CPU loop is still running"
            opened, = context.paper_engine.open_orders.values()
            assert opened.approval_id and opened.risk_check_id
            assert await context.orchestrator.on_candle_closed(rows[-1]) is None
            output = await asyncio.wait_for(task, settings.office_shadow_timeout_seconds + 4)
            assert output.status == AgentStatus.TIMEOUT
            assert all(child.pid != child_pid for child in context.agent_runtime.shadow_runner._children)
            close = opened.entry_price
            closed, = await context.paper_engine.on_candle(Candle(
                exchange=Exchange.BINANCE, symbol=opened.symbol, timeframe=timeframe,
                open=close, close=close, high=max(close, opened.take_profit) * 1.00001,
                low=min(close, opened.take_profit) * .99999, volume=100,
                closed_at=datetime.now(timezone.utc)))
            assert closed.exit_reason == "TAKE_PROFIT"
            balance = context.paper_engine.balance
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert not restored.paper_engine.open_orders
        assert restored.paper_engine.balance == balance
        assert restored.risk_manager.state.open_positions == 0
        assert any(order.model_dump() == closed.model_dump()
                   for order in restored.paper_engine.closed_orders)
    return {"timeframe": timeframe, "cpu_loop_started": True, "cpu_child_terminated": True,
        "paper_order_completed_while_cpu_loop_running": True, "decision_elapsed_ms": round(elapsed_ms, 3),
        "risk_and_oms_required": True, "duplicate_rejected": True, "rebuild_preserved_closed_order": True,
        "fixture_scope": "one forced CPU-bound SHADOW plus normal analytical admission",
        "execution_environment": "PAPER", "live_execution_attempted": False}


async def certify_dependency_fault(settings, fault):
    """Inject an explicit dependency exception, not a claim of network chaos."""
    if fault not in {"database", "broker", "audit"}:
        raise ValueError("CERTIFICATION_FAULT_REFUSED")
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    reached = []
    async with app.router.lifespan_context(app):
        assert not context.paper_engine.open_orders
        rows = probe_candles("15m")
        await seed_probe(context, rows)
        rows[-1] = fresh_fixture_close(rows[-1], rows[-2])
        async def unavailable(*args, **kwargs):
            reached.append(True)
            error = AuditError if fault == "audit" else DatabaseError
            raise error("Isolated certification dependency fault")
        target, method = {
            "database": (context.repository, "save_candle"),
            "broker": (context.event_bus, "publish"),
            "audit": (context.audit_service, "record"),
        }[fault]
        with patch.object(target, method, unavailable):
            await context.orchestrator.on_candle_closed(rows[-1])
        assert reached, "The intended dependency boundary must actually be reached"
        assert not context.paper_engine.open_orders
        assert context.risk_manager.state.open_positions == 0
    return {"fault": fault, "injection": "dependency method raises an explicit exception",
        "boundary_reached": True, "new_paper_orders": 0, "fail_closed": True,
        "execution_environment": "PAPER", "live_execution_attempted": False}

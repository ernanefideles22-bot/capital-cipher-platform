"""Full PAPER application flow with real agents, guards, HTTP and durable orders."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from math import exp, sin
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.config import Settings
from app.main import create_app
from app.market_data.clock import evaluate_clock_probe
from app.schemas.common import Exchange
from datetime import timedelta
from app.schemas.market import Candle
from app.schemas.decisions import Decision
from app.core.errors import RiskError


def trust_fixture_clock(context):
    now = datetime.now(timezone.utc)
    context.clock_registry.record(Exchange.BINANCE, evaluate_clock_probe(
        source="isolated-fixture", request_started_at=now,
        source_at=now, response_received_at=now,
    ))


async def validate_full_paper_flow(settings: Settings, direction: int, expected_side: str) -> dict:
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert len(context.agent_registry.registrations()) == 300
        assert settings.require_trusted_market_clock
        assert context.oms_service.target_environment.value == "PAPER"
        assert not context.paper_engine.open_orders
        # With no trusted clock evidence, the entire cycle must fail closed.
        now = datetime.now(timezone.utc)
        candles = []
        for index in range(220):
            close = 100 * exp(direction * .002 * index) * (1 + direction * .01 * sin(1.1 * index))
            candles.append(Candle(
                exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe="15m",
                open=close * .999, high=close * 1.003, low=close * .997,
                close=close, volume=100,
                closed_at=now - timedelta(minutes=15 * (219 - index)),
            ))
        assert await context.orchestrator.on_candle_closed(candles[-1]) is None
        assert not context.paper_engine.open_orders
        trust_fixture_clock(context)
        for candle in candles[:-1]:
            context.candle_store.add(candle)
        await context.repository.save_candles(candles[:-1])
        decision = await context.orchestrator.on_candle_closed(candles[-1])
        assert decision is not None, list(context.orchestrator.failures)
        assert decision.candidate_action.value == expected_side, decision.model_dump()
        assert decision.risk_status.value in {"APPROVED", "REDUCED"}, decision.model_dump()
        assert len(context.paper_engine.open_orders) == 1
        opened, = context.paper_engine.open_orders.values()
        assert opened.side.value == expected_side
        assert opened.approval_id and opened.risk_check_id
        assert await context.orchestrator.on_candle_closed(candles[-1]) is None
        assert len(context.paper_engine.open_orders) == 1
        if settings.agent_worker_enabled:
            deadline = asyncio.get_running_loop().time() + 240
            while True:
                jobs = [job for job in await context.agent_runtime.list_jobs(limit=1000)
                        if job.correlation_id == decision.correlation_id]
                if len(jobs) == 300 and all(job.status == "COMPLETED" for job in jobs):
                    break
                assert not any(job.status == "DEAD_LETTER" for job in jobs), "Agent entered dead letter"
                assert asyncio.get_running_loop().time() < deadline, "300-agent worker drain timed out"
                await asyncio.sleep(.25)
        else:
            jobs = [job for job in await context.agent_runtime.list_jobs(limit=1000)
                    if job.correlation_id == decision.correlation_id]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/paper/orders/{opened.paper_order_id}")
            assert response.json()["data"]["order"] == opened.model_dump(mode="json")
            chain = (await client.get(f"/api/v1/audit/correlation/{decision.correlation_id}")).json()["data"]["chain"]
            assert {"DECISION_CANDIDATE", "RISK_CHECK", "PAPER_ORDER_CREATED"} <= {e["audit_type"] for e in chain}

    # Rebuild every application component and reopen the same durable file.
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert restored.paper_engine.open_orders[opened.paper_order_id].model_dump() == opened.model_dump()
        assert restored.risk_manager.state.open_positions == 1
        close = opened.entry_price
        closing_candle = Candle(
            exchange=Exchange.BINANCE, symbol=opened.symbol, timeframe="15m",
            open=close, close=close, volume=100,
            high=max(close, opened.take_profit) * 1.00001,
            low=min(close, opened.take_profit) * .99999,
            closed_at=datetime.now(timezone.utc),
        )
        closed, = await restored.paper_engine.on_candle(closing_candle)
        assert closed.exit_reason == "TAKE_PROFIT"
        assert closed.pnl > 0
        balance = restored.paper_engine.balance
        performance = restored.paper_engine.performance().model_dump(mode="json")

    restarted = build_context(settings, with_database=True)
    app = create_app(restarted, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert restarted.paper_engine.balance == balance
        assert not restarted.paper_engine.open_orders
        assert next(o for o in restarted.paper_engine.closed_orders if o.paper_order_id == closed.paper_order_id).model_dump() == closed.model_dump()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            actual = (await client.get(f"/api/v1/paper/orders/{closed.paper_order_id}")).json()["data"]["order"]
            assert actual == closed.model_dump(mode="json")
            assert (await client.get("/api/v1/paper/performance")).json()["data"] == performance
            assert (await client.get("/api/v1/status")).json()["data"]["mode"] == "PAPER"
            decision_response = (await client.get(f"/api/v1/decisions/{decision.decision_id}")).json()
            assert decision_response["success"], decision_response
            assert decision_response["data"]["decision"] == decision.model_dump(mode="json")
            chain = (await client.get(f"/api/v1/audit/correlation/{decision.correlation_id}")).json()["data"]["chain"]
            assert {"DECISION_CANDIDATE", "RISK_CHECK", "PAPER_ORDER_CREATED", "PAPER_ORDER_CLOSED"} <= {e["audit_type"] for e in chain}
    return {
        "side": expected_side, "decision_id": decision.decision_id,
        "correlation_id": decision.correlation_id, "paper_order_id": closed.paper_order_id,
        "confidence": decision.confidence, "risk_status": decision.risk_status.value,
        "exit_reason": closed.exit_reason, "exit_price": closed.exit_price,
        "pnl": closed.pnl, "balance": balance, "registered_agents": 300,
        "completed_agent_jobs": sum(job.status == "COMPLETED" for job in jobs),
        "shadow_dependency_skips": sum(job.output is not None and job.output.status.value == "SKIPPED" for job in jobs),
        "open_restart_verified": True, "closed_restart_verified": True,
        "duplicate_rejected": True, "untrusted_clock_rejected": True,
        "durable_decision_and_audit_verified": True,
        "execution_environment": "PAPER", "live_execution_attempted": False,
    }


async def validate_paper_safety(settings: Settings) -> dict:
    """Run destructive-looking controls only inside the caller's isolated DB."""
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        baseline = context.paper_engine.performance().model_dump(mode="json")
        trust_fixture_clock(context)
        now = datetime.now(timezone.utc)
        stale = Candle(
            exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe="15m",
            open=100, high=101, low=99, close=100, volume=100,
            closed_at=now - timedelta(days=2),
        )
        context.candle_store.add(stale.model_copy(update={"closed_at": now}))
        assert await context.orchestrator.on_candle_closed(stale) is None
        assert context.paper_engine.performance().model_dump(mode="json") == baseline
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post("/api/v1/risk/kill-switch", json={"reason": "unauthenticated fixture"})
            assert denied.status_code in {401, 403}
            assert not context.risk_manager.control_state.active
            activated = await client.post(
                "/api/v1/risk/kill-switch", json={"reason": "isolated PAPER validation"},
                headers={"X-API-Key": settings.admin_api_key},
            )
            assert activated.status_code == 200 and activated.json()["success"]
        decision = Decision(
            correlation_id=str(uuid4()), symbol="BTCUSDT", timeframe="15m",
            candidate_action="BUY", confidence=95,
        )
        await context.repository.save_decision(decision)
        check = await context.risk_manager.check(decision, entry_price=100, atr=1)
        assert not check.approved
        try:
            await context.paper_engine.create_order(decision, check, current_price=100)
        except RiskError:
            pass
        else:
            raise AssertionError("PAPER order accepted without approved risk")
        assert not context.paper_engine.open_orders
    restored = build_context(settings, with_database=True)
    app = create_app(restored, with_market_data=False)
    async with app.router.lifespan_context(app):
        assert restored.risk_manager.control_state.active
        assert restored.state_machine.state.value == "ERROR"
        assert not restored.state_machine.can_operate()
        assert restored.paper_engine.performance().model_dump(mode="json") == baseline
    return {
        "out_of_order_data_rejected": True, "unauthenticated_control_rejected": True,
        "unapproved_order_rejected": True, "kill_switch_durable": True,
        "isolated_final_state": "ERROR", "main_staging_kill_switch_changed": False,
    }

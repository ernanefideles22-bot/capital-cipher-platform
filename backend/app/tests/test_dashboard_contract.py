"""Read-only dashboard/backend contract checks in isolated PAPER mode."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.config import Settings
from app.main import create_app


@pytest.mark.parametrize(
    ("path", "required_keys"),
    [
        ("/api/v1/status", {"mode", "database", "oms", "kill_switch_active"}),
        ("/api/v1/agents/status", {"agents"}),
        ("/api/v1/decisions", {"decisions"}),
        ("/api/v1/risk/status", {"kill_switch_active", "open_positions", "blocked_operations"}),
        ("/api/v1/risk/limits", set()),
        ("/api/v1/paper/orders", {"orders"}),
        ("/api/v1/paper/performance", {"balance", "net_pnl"}),
        ("/api/v1/market/candles?symbol=BTCUSDT&timeframe=15m&limit=200", {"candles"}),
        ("/api/v1/audit/events", {"events"}),
        ("/api/v1/reports/agents/ranking", {"ranking"}),
        ("/api/v1/backtest/reports", {"reports"}),
    ],
)
async def test_dashboard_readonly_contract(path, required_keys):
    context = build_context(Settings(DATABASE_URL="sqlite+aiosqlite:///:memory:"), with_database=False)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    assert isinstance(body["data"], dict), body
    assert required_keys <= body["data"].keys(), body
    if path == "/api/v1/status":
        assert body["data"]["mode"] == "PAPER"
        assert body["data"]["oms"]["live_execution_available"] is False


async def test_kill_switch_is_reflected_in_dashboard_status_and_blocks_risk():
    from app.schemas.common import RiskStatus
    from app.tests.conftest import make_decision

    context = build_context(Settings(DATABASE_URL="sqlite+aiosqlite:///:memory:"), with_database=False)
    app = create_app(context, with_market_data=False)
    async with app.router.lifespan_context(app):
        await context.state_machine.trigger_kill_switch(reason="dashboard safety test", actor="test")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = (await client.get("/api/v1/status")).json()
            risk = (await client.get("/api/v1/risk/status")).json()
            orders = (await client.get("/api/v1/paper/orders")).json()

        check = await context.risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)

    assert status["data"]["mode"] != "LIVE"
    assert status["data"]["kill_switch_active"] is True
    assert status["data"]["oms"]["live_execution_available"] is False
    assert risk["data"]["kill_switch_active"] is True
    assert orders["data"]["orders"] == []
    assert check.approved is False
    assert check.risk_status == RiskStatus.KILL_SWITCH


async def test_offline_system_is_not_reported_as_operational():
    from app.core.state_machine import SystemState
    from app.schemas.common import RiskStatus
    from app.tests.conftest import make_decision

    context = build_context(Settings(DATABASE_URL="sqlite+aiosqlite:///:memory:"), with_database=False)
    assert context.state_machine.state == SystemState.OFFLINE
    assert context.state_machine.can_operate() is False

    check = await context.risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.approved is False
    assert check.risk_status == RiskStatus.BLOCKED
    assert not context.paper_engine.open_orders

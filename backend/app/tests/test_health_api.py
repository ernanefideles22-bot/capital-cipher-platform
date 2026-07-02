"""API tests (docs/22): health, status, standard envelope, security."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
async def client():
    settings = get_settings()
    context = build_context(settings, with_database=False)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            app.state.context = context
            yield ac


async def test_health_returns_200(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "capital-cipher-api"


async def test_status_returns_paper_mode(client):
    response = await client.get("/api/v1/status")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["mode"] == "PAPER"
    assert body["meta"]["request_id"]


async def test_response_envelope_is_stable(client):
    response = await client.get("/api/v1/market/symbols")
    body = response.json()
    assert set(body.keys()) == {"success", "data", "error", "meta"}
    assert body["data"]["symbols"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def test_kill_switch_requires_auth(client):
    response = await client.post("/api/v1/risk/kill-switch", json={"reason": "test"})
    assert response.status_code in (401, 403)


async def test_unknown_decision_returns_not_found_error(client):
    response = await client.get("/api/v1/decisions/nonexistent-id")
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"

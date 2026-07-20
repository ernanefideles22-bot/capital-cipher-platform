"""HTTP boundary tests for authentication, rate limits, and safe modes."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.config import Settings
from app.main import create_app


@asynccontextmanager
async def security_client(settings: Settings):
    context = build_context(settings, with_database=False)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/orchestrator/evaluate", {}),
        ("POST", "/api/v1/backtest/run", {}),
        ("POST", "/api/v1/backtest/walk-forward", {}),
        ("POST", "/api/v1/market/datasets", {}),
        ("POST", "/api/v1/market/gaps/scan", {}),
        ("POST", "/api/v1/market/backfills", {}),
        (
            "GET",
            f"/api/v1/market/backfills/{'a' * 64}/lineage",
            {},
        ),
        ("POST", "/api/v1/risk/kill-switch", {"reason": "security test"}),
    ],
)
async def test_state_changing_endpoints_fail_closed_without_admin_key(
    method: str, path: str, body: dict
):
    async with security_client(Settings()) as client:
        response = await client.request(method, path, json=body)
    assert response.status_code == 403


async def test_admin_key_uses_authenticated_boundary():
    admin_key = "a" * 32
    async with security_client(Settings(ADMIN_API_KEY=admin_key)) as client:
        denied = await client.post("/api/v1/orchestrator/evaluate", json={})
        accepted = await client.post(
            "/api/v1/orchestrator/evaluate",
            headers={"X-API-Key": admin_key},
            json={},
        )
    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["error"]["code"] == "MARKET_DATA_UNAVAILABLE"


async def test_rate_limit_is_enforced_and_health_is_exempt():
    settings = Settings(API_RATE_LIMIT_PER_MINUTE=2)
    async with security_client(settings) as client:
        assert (await client.get("/api/v1/status")).status_code == 200
        assert (await client.get("/api/v1/status")).status_code == 200
        limited = await client.get("/api/v1/status")
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert (await client.get("/health")).status_code == 200


async def test_unauthenticated_websocket_streams_are_not_exposed():
    settings = Settings()
    context = build_context(settings, with_database=False)
    app = create_app(context, with_market_data=False)
    exposed_paths = {
        getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", "").startswith("/ws/")
    }
    assert exposed_paths == set()


async def test_body_limit_and_security_headers():
    settings = Settings(MAX_REQUEST_BODY_BYTES=16)

    async def streamed_body():
        yield b'{"candles":['
        yield b"1234567890]}"

    async with security_client(settings) as client:
        oversized = await client.post(
            "/api/v1/backtest/run",
            headers={"Content-Type": "application/json"},
            content=streamed_body(),
        )
        response = await client.get("/api/v1/status")
    assert oversized.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_short_admin_key_is_rejected():
    with pytest.raises(ValueError):
        Settings(ADMIN_API_KEY="short-placeholder")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.binance.test",
        "https://user:password@api.binance.test",
        "file:///tmp/provider",
        "https://api.binance.test?redirect=http://127.0.0.1",
    ],
)
def test_public_market_provider_urls_fail_closed(url: str):
    with pytest.raises(ValueError):
        Settings(BINANCE_PUBLIC_REST_URL=url)

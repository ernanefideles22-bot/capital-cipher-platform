"""Chart GETs stay independent of S3 capture, account secrets and execution."""
import httpx
import pytest

from app.demo.gate_service import create_app


def app(fetch):
    # No lifespan: pure HTTP chart reads must work without any account/runtime.
    return create_app(environment={}, market_fetch=fetch)


@pytest.mark.asyncio
async def test_chart_is_fixed_venue_closed_only_bounded_and_cached():
    calls = []
    async def fetch(symbol, timeframe, *, limit):
        calls.append((symbol, timeframe, limit))
        return [dict(close_time_ms=(i + 1) * 900_000, open=100, high=102, low=99,
                     close=101, volume=5) for i in range(200)]
    application = app(fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        for _ in range(2):
            response = await client.get('/api/gate/market/candles?symbol=ETHUSDT&timeframe=15m')
            assert response.status_code == 200
            data = response.json()
            assert data['source'] == 'GATE' and data['environment'] == 'GATE_TESTNET'
            assert data['closed_candles_only'] and data['execution_authorized'] is False
            assert len(data['candles']) == 200
            assert response.headers['cache-control'] == 'no-store'
        assert calls == [('ETHUSDT', '15m', 200)]
        assert (await client.get('/api/gate/status')).json()['forward_research']['status'] == 'DISABLED'


@pytest.mark.asyncio
async def test_invalid_chart_requests_never_fetch_and_cannot_write():
    async def forbidden(*args, **kwargs):
        raise AssertionError('No network for invalid requests')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(forbidden)), base_url="http://test") as client:
        for query in ('symbol=DOGEUSDT', 'symbol=https://evil.invalid', 'timeframe=5m', 'timeframe=1d'):
            assert (await client.get('/api/gate/market/candles?' + query)).status_code == 422
        for method in ('POST', 'PUT', 'DELETE'):
            assert (await client.request(method, '/api/gate/market/candles')).status_code == 405


@pytest.mark.asyncio
async def test_chart_error_is_sanitized_without_cross_venue_or_stale_fallback():
    async def broken(*args, **kwargs):
        raise RuntimeError('private-key-or-upstream-response')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app(broken)), base_url="http://test") as client:
        response = await client.get('/api/gate/market/candles')
        assert response.status_code == 502
        assert response.json() == {'detail': 'GATE_CHART_DATA_UNAVAILABLE'}

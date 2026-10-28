"""Network boundary, signing, privacy and connection-readiness regression tests."""
from __future__ import annotations

import copy
import hashlib
import hmac
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from app.demo.binance import BinanceFuturesDemoClient, DemoCredentials, DemoError, KEY_VARIABLE, SECRET_VARIABLE
from app.demo.main import ACKNOWLEDGEMENT, create_app, validate_environment

NOW = 1800000000000
KEY = "demo-key-123456789"
SECRET = "demo-signing-secret-123456789"
ADMIN = "admin-demo-local-test-123456789ABCDEFGHIJ"


def environment(*, keys=False):
    values = {
        "DEMO_RESOURCE_NAMESPACE": "capital-cipher-binance-futures-demo",
        "DEMO_ACKNOWLEDGEMENT": ACKNOWLEDGEMENT,
        "DEMO_ADMIN_API_KEY": ADMIN,
        "DEMO_EXECUTION_ENABLED": "false",
    }
    if keys:
        values.update({KEY_VARIABLE: KEY, SECRET_VARIABLE: SECRET})
    return values


def payloads():
    # Account V3 intentionally does NOT contain canTrade: it comes from accountConfig.
    return {
        "/fapi/v1/time": {"serverTime": NOW},
        "/fapi/v3/account": {"assets": [{"asset": "USDT", "walletBalance": "5000", "unrealizedProfit": "0", "marginBalance": "5000", "availableBalance": "5000"}]},
        "/fapi/v3/positionRisk": [],
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": [],
        "/fapi/v1/accountConfig": {"canTrade": True, "dualSidePosition": False, "multiAssetsMargin": False},
        "/fapi/v1/klines": [[NOW-60000, "10", "12", "9", "11", "20", NOW-1], [NOW, "11", "13", "10", "12", "20", NOW+59999]],
    }


def handler_for(data, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "demo-fapi.binance.com"
        return httpx.Response(200, json=copy.deepcopy(data[request.url.path]))
    return handler


@pytest.mark.asyncio
async def test_signatures_are_correct_and_every_request_stays_on_demo_gets():
    requests = []
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET), transport=httpx.MockTransport(handler_for(payloads(), requests)), clock_ms=lambda: NOW)
    try:
        result = await client.validate_account()
    finally:
        await client.aclose()
    assert result["account_preflight_passed"] is True
    assert result["equity_usdt"] == 5000
    assert result["execution_enabled"] is False
    assert result["api_trade_permission_verified"] is False
    assert len(requests) == 6
    for request in requests:
        if request.url.path.endswith("/time"):
            assert "X-MBX-APIKEY" not in request.headers
            continue
        pairs = list(request.url.params.multi_items())
        assert pairs[-1][0] == "signature"
        expected = hmac.new(SECRET.encode(), urlencode(pairs[:-1]).encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(pairs[-1][1], expected)
        assert request.headers["X-MBX-APIKEY"] == KEY
        assert dict(pairs)["recvWindow"] == "5000"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,signed", [('/fapi/v1/order', True), ('/fapi/v1/leverage', True), ('/fapi/v1/algoOrder', True), ('https://fapi.binance.com/fapi/v3/account', True), ('//evil.test', False), ('/fapi/v3/account', False)])
async def test_unknown_and_write_endpoints_block_before_network(path, signed):
    def forbidden(request):
        pytest.fail("Unexpected network request")
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET), transport=httpx.MockTransport(forbidden))
    try:
        with pytest.raises(DemoError, match="ENDPOINT_BLOCKED"):
            await client._get(path, signed=signed)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redirect_never_forwards_api_key_or_signature():
    seen = []
    def redirect(request):
        seen.append(request)
        if request.url.path.endswith('/time'):
            return httpx.Response(200, json={"serverTime": NOW})
        return httpx.Response(302, headers={"location": "https://fapi.binance.com/fapi/v3/account"})
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET), transport=httpx.MockTransport(redirect), clock_ms=lambda: NOW)
    try:
        with pytest.raises(DemoError, match="REDIRECT_BLOCKED"):
            await client.validate_account()
    finally:
        await client.aclose()
    assert len(seen) == 2
    assert all(r.url.host == "demo-fapi.binance.com" for r in seen)


@pytest.mark.asyncio
async def test_missing_keys_and_unsynchronized_clock_block_private_gets():
    client = BinanceFuturesDemoClient(None)
    try:
        with pytest.raises(DemoError, match="CREDENTIALS_MISSING"):
            await client._get('/fapi/v3/account', signed=True)
    finally:
        await client.aclose()
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET))
    try:
        with pytest.raises(DemoError, match="CLOCK_NOT_SYNCHRONIZED"):
            await client._get('/fapi/v3/account', signed=True)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['hedge', 'multi_asset', 'no_funds', 'orders', 'algo_orders', 'position', 'permission'])
async def test_account_review_requirements_do_not_mutate_the_venue(kind):
    data = payloads()
    if kind == 'hedge':
        data['/fapi/v1/accountConfig']['dualSidePosition'] = True
    elif kind == 'multi_asset':
        data['/fapi/v1/accountConfig']['multiAssetsMargin'] = True
    elif kind == 'no_funds':
        data['/fapi/v3/account']['assets'][0]['availableBalance'] = '0'
    elif kind == 'orders':
        data['/fapi/v1/openOrders'] = [{'orderId': 123}]
    elif kind == 'algo_orders':
        data['/fapi/v1/openAlgoOrders'] = [{'algoId': 123}]
    elif kind == 'position':
        data['/fapi/v3/positionRisk'] = [{'symbol': 'BTCUSDT', 'positionAmt': '-0.01', 'entryPrice': '80000', 'unRealizedProfit': '-2', 'positionSide': 'BOTH'}]
    else:
        data['/fapi/v1/accountConfig']['canTrade'] = False
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET), transport=httpx.MockTransport(handler_for(data)), clock_ms=lambda: NOW)
    try:
        result = await client.validate_account()
    finally:
        await client.aclose()
    assert result['authenticated']
    assert not result['account_preflight_passed']
    assert result['issues']
    assert result['execution_enabled'] is False
    if kind == 'position':
        assert result['positions'][0]['side'] == 'SELL'
        assert result['positions'][0]['quantity'] == 0.01


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', 'bad'])
async def test_invalid_balance_never_passes_preflight(value):
    data = payloads()
    data['/fapi/v3/account']['assets'][0]['marginBalance'] = value
    client = BinanceFuturesDemoClient(DemoCredentials(KEY, SECRET), transport=httpx.MockTransport(handler_for(data)), clock_ms=lambda: NOW)
    try:
        with pytest.raises(DemoError, match='BALANCE_OR_POSITION_INVALID'):
            await client.validate_account()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_only_closed_futures_candles_are_returned():
    client = BinanceFuturesDemoClient(None, transport=httpx.MockTransport(handler_for(payloads())), clock_ms=lambda: NOW)
    try:
        rows = await client.candles(symbol='BTCUSDT', timeframe='1m')
        assert len(rows) == 1
        assert rows[0]['close_time_ms'] < NOW
        with pytest.raises(DemoError, match='SYMBOL_BLOCKED'):
            await client.candles(symbol='EVIL/USDT', timeframe='1m')
    finally:
        await client.aclose()


@pytest.mark.parametrize('name,value', [('DATABASE_URL','postgresql://paper'), ('REDIS_URL','redis://paper'), ('MIGRATION_DATABASE_URL','postgresql://paper'), ('DEMO_EXECUTION_ENABLED','true'), ('DEMO_ACKNOWLEDGEMENT',''), ('DEMO_RESOURCE_NAMESPACE','paper'), ('DEMO_ADMIN_API_KEY','weak'), (KEY_VARIABLE,KEY)])
def test_isolation_and_execution_guard(name, value):
    env = environment()
    env[name] = value
    with pytest.raises(RuntimeError):
        validate_environment(env)


def test_public_status_and_page_never_expose_keys_or_balance():
    app = create_app(environment=environment(keys=True), transport=httpx.MockTransport(handler_for(payloads())), clock_ms=lambda: NOW)
    with TestClient(app) as client:
        client.post('/api/demo/validate', headers={'X-API-Key': ADMIN})
        result = client.get('/api/demo/status')
        assert result.status_code == 200
        assert result.json()['authenticated']
        for secret in (KEY, SECRET, ADMIN, 'wallet_balance_usdt', 'equity_usdt'):
            assert secret not in result.text
            assert secret not in client.get('/').text
        assert client.get('/api/demo/account').status_code == 401
        account = client.get('/api/demo/account', headers={'X-API-Key': ADMIN})
        assert account.json()['equity_usdt'] == 5000
        assert client.get('/ready').json()['execution_enabled'] is False
        assert client.post('/api/demo/order', headers={'X-API-Key': ADMIN}).status_code == 404
        assert client.get('/health').json()['status'] == 'UP'
        assert client.get('/api/demo/status').headers['cache-control'] == 'no-store'


def test_missing_keys_start_healthy_but_never_ready():
    app = create_app(environment=environment(), transport=httpx.MockTransport(handler_for(payloads())), clock_ms=lambda: NOW)
    with TestClient(app) as client:
        client.post('/api/demo/validate', headers={'X-API-Key': ADMIN})
        assert client.get('/health').status_code == 200
        assert client.get('/ready').status_code == 503
        assert client.get('/api/demo/status').json()['status'] == 'WAITING_FOR_CREDENTIALS'


def test_failed_revalidation_clears_old_account_and_readiness():
    data = payloads()
    available = True
    def handler(request):
        if not available:
            return httpx.Response(403, text=SECRET)
        return handler_for(data)(request)
    app = create_app(environment=environment(keys=True), transport=httpx.MockTransport(handler), clock_ms=lambda: NOW)
    with TestClient(app) as client:
        client.post('/api/demo/validate', headers={'X-API-Key': ADMIN})
        assert client.get('/ready').status_code == 200
        available = False
        r = client.post('/api/demo/validate', headers={'X-API-Key': ADMIN})
        assert client.get('/ready').status_code == 503
        assert client.get('/api/demo/account', headers={'X-API-Key': ADMIN}).status_code == 503
        assert SECRET not in r.text
        assert r.json()['error_code'] == 'DEMO_AUTH_OR_ACCESS_REJECTED'


def test_credentials_repr_is_redacted():
    assert KEY not in repr(DemoCredentials(KEY, SECRET))
    assert SECRET not in repr(DemoCredentials(KEY, SECRET))

"""The dashboard can read only the pinned Gate public service, without keys."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import gate


def application():
    app = FastAPI()
    app.include_router(gate.router, prefix='/api/v1')
    return app


def mock_gate(monkeypatch, handler):
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs == dict(base_url='https://capital-cipher-gate-testnet-staging.up.railway.app',
                              timeout=8, follow_redirects=False, trust_env=False)
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gate.httpx, 'AsyncClient', client)


def test_main_application_mounts_only_public_gate_reads():
    from app.main import create_app
    from app.core.config import Settings
    app = create_app(SimpleNamespace(settings=Settings(_env_file=None, APP_ENV='test')), with_market_data=False)
    paths = app.openapi()['paths']
    for path in ('status', 'research/forward', 'market/candles'):
        assert set(paths['/api/v1/gate/' + path]) == {'get'}


def test_get_uses_literal_origin_and_never_forwards_browser_credentials(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert str(request.url) == 'https://capital-cipher-gate-testnet-staging.up.railway.app/api/gate/status'
        assert not ({'authorization', 'x-api-key', 'cookie'} & set(request.headers))
        return httpx.Response(200, json={'environment': 'GATE_TESTNET', 'execution_enabled': False})
    mock_gate(monkeypatch, handler)
    with TestClient(application()) as client:
        result = client.get('/api/v1/gate/status', headers={
            'X-API-Key': 'browser-private-value', 'Authorization': 'Bearer browser-value',
            'Cookie': 'private=value',
        })
        assert result.status_code == 200
        assert result.json()['data']['execution_enabled'] is False
    assert len(calls) == 1


@pytest.mark.parametrize('code', [301, 302, 307, 308, 404, 500, 503])
def test_redirects_and_failures_do_not_fallback_or_leak_upstream_body(monkeypatch, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(code, headers={'Location': 'https://evil.invalid'},
                              text='private upstream diagnostics')
    mock_gate(monkeypatch, handler)
    with TestClient(application()) as client:
        result = client.get('/api/v1/gate/status')
        assert result.status_code in {502, 503}
        assert 'private' not in result.text
        assert len(calls) == 1


@pytest.mark.parametrize('payload', [[], None, {'environment': 'BINANCE'}, {'environment': 'GATE_LIVE'}])
def test_wrong_identity_or_shape_is_not_gate_telemetry(monkeypatch, payload):
    mock_gate(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with TestClient(application()) as client:
        assert client.get('/api/v1/gate/status').status_code == 502


def test_chart_market_identity_and_query_are_checked(monkeypatch):
    def handler(request):
        assert request.url.path == '/api/gate/market/candles'
        assert dict(request.url.params) == {'symbol': 'SOLUSDT', 'timeframe': '4h'}
        return httpx.Response(200, json=dict(source='GATE', environment='GATE_TESTNET',
            market='GATE_USDT_FUTURES_TESTNET', symbol='SOLUSDT', timeframe='4h',
            closed_candles_only=True, candles=[]))
    mock_gate(monkeypatch, handler)
    with TestClient(application()) as client:
        assert client.get('/api/v1/gate/market/candles?symbol=SOLUSDT&timeframe=4h').status_code == 200


def test_unsupported_markets_and_write_paths_never_reach_gate(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('No upstream request permitted')
    monkeypatch.setattr(gate, 'read_gate', forbidden)
    with TestClient(application()) as client:
        assert client.get('/api/v1/gate/market/candles?symbol=DOGEUSDT').status_code == 422
        assert client.get('/api/v1/gate/market/candles?timeframe=5m').status_code == 422
        for action in ('execute', 'canary', 'reset', 'automatic', 'close', 'orders', 'account'):
            assert client.post('/api/v1/gate/' + action).status_code == 404
        for action in ('status', 'research/forward', 'market/candles'):
            assert client.post('/api/v1/gate/' + action).status_code == 405


async def test_unknown_internal_read_path_fails_before_client_creation():
    with pytest.raises(Exception) as caught:
        await gate.read_gate('/api/gate/account')
    assert caught.value.status_code == 404


def test_financial_bridge_preserves_missing_data_without_synthesizing_zero(monkeypatch):
    mock_gate(monkeypatch, lambda request: httpx.Response(200, json={
        'collection': {'status': 'DISABLED'}, 'comparison': None, 'execution_authorized': False,
    }))
    with TestClient(application()) as client:
        data = client.get('/api/v1/gate/research/forward').json()['data']
        assert data['comparison'] is None
        assert 'pnl' not in str(data).lower()

"""Demo dashboard proxy authorization and strict server-only target tests."""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import binance_demo

ADMIN='main-admin-strong-key-1234567890ABCDEFG'


def app_context():
    return SimpleNamespace(settings=SimpleNamespace(admin_api_key=ADMIN,demo_connector_url='http://capital-cipher-binance-demo.railway.internal:8080',demo_connector_key='server-only-connector-key'),risk_manager=SimpleNamespace(kill_switch_active=False))


def application():
    app=FastAPI();app.state.context=app_context();app.include_router(binance_demo.router,prefix='/api/v1');return app


def test_private_snapshot_and_all_execution_controls_require_main_admin_key(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('Unauthenticated request reached connector')
    monkeypatch.setattr(binance_demo,'forward',forbidden)
    with TestClient(application()) as client:
        assert client.get('/api/v1/binance-demo/snapshot').status_code==401
        for action in ('analysis','execute','close','automatic','kill','reconcile','reset','canary'):
            assert client.post('/api/v1/binance-demo/'+action,json={}).status_code==401


def test_public_connection_status_has_no_account_or_connector_key(monkeypatch):
    async def forward(context,method,path,body=None):
        assert method=='GET' and path=='/api/demo/status'
        return {'authenticated':True,'automatic_enabled':False}
    monkeypatch.setattr(binance_demo,'forward',forward)
    with TestClient(application()) as client:
        response=client.get('/api/v1/binance-demo/status')
        assert response.json()['data']['authenticated']
        assert 'server-only-connector-key' not in response.text


def test_central_kill_switch_blocks_manual_and_automatic_demo_entries(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('Blocked entry reached connector')
    monkeypatch.setattr(binance_demo,'forward',forbidden)
    app=application();app.state.context.risk_manager.kill_switch_active=True
    with TestClient(app) as client:
        headers={'X-API-Key':ADMIN}
        assert client.post('/api/v1/binance-demo/execute',headers=headers,json={'proposal_id':'proposal-12345678'}).status_code==409
        assert client.post('/api/v1/binance-demo/automatic',headers=headers,json={'enabled':True}).status_code==409
        assert client.post('/api/v1/binance-demo/canary',headers=headers,json={}).status_code==409


@pytest.mark.asyncio
async def test_proxy_blocks_unapproved_hosts_before_reading_or_sending_keys():
    context=app_context();context.settings.demo_connector_url='https://evil.invalid'
    with pytest.raises(Exception) as caught:await binance_demo.forward(context,'GET','/api/demo/account')
    assert caught.value.status_code==503


def test_execute_route_forwards_only_stored_proposal_identity(monkeypatch):
    calls=[]
    async def forward(context,method,path,body=None):
        calls.append((method,path,body));return {'state':'PROTECTED'}
    monkeypatch.setattr(binance_demo,'forward',forward)
    with TestClient(application()) as client:
        r=client.post('/api/v1/binance-demo/execute',headers={'X-API-Key':ADMIN},json={'proposal_id':'proposal-12345678','quantity':99999,'host':'evil'})
        assert r.status_code==200
        assert calls==[('POST','/api/demo/execute',{'proposal_id':'proposal-12345678'})]

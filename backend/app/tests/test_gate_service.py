import time

from fastapi.testclient import TestClient

from app.demo.gate_service import KEY_VARIABLE, SECRET_VARIABLE, create_app
from app.demo.gate_readonly import GateProbeError


async def fake_market(*args, **kwargs):
    return [{}] * 60


async def fake_inventory(*args, **kwargs):
    return {'positions':0,'orders':0,'conditional_orders':0,'atomic':False,'execution_authorized':False}


async def fake_position_config(*args, **kwargs):
    return {'mode':'single','margin':'isolated','leverage':'1','one_way_isolated_1x':True,'execution_authorized':False}


def status_after_refresh(client):
    for _ in range(100):
        data = client.get('/api/gate/status').json()
        if data['checked_at']:
            return data
        time.sleep(.01)
    raise AssertionError('refresh did not run')


def test_missing_credentials_do_not_probe():
    def probe(*args):
        raise AssertionError('must not call Gate')
    with TestClient(create_app(environment={}, probe=probe, market_fetch=fake_market, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        assert status_after_refresh(client)['status'] == 'WAITING_FOR_CREDENTIALS'
        assert client.post('/api/demo/execute').status_code == 404


def test_success_exposes_status_only():
    env = {KEY_VARIABLE: 'private-key', SECRET_VARIABLE: 'private-secret'}
    with TestClient(create_app(environment=env, probe=lambda *args: {'total': '123'}, market_fetch=fake_market, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        data = status_after_refresh(client)
        assert data['status'] == 'CONNECTED_READ_ONLY'
        assert data['execution_enabled'] is False
        assert 'private' not in str(data) and '123' not in str(data)


def test_failure_never_leaks_credentials():
    def probe(*args):
        raise GateProbeError('private-secret')
    env = {KEY_VARIABLE: 'private-key', SECRET_VARIABLE: 'private-secret'}
    with TestClient(create_app(environment=env, probe=probe, market_fetch=fake_market, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        data = status_after_refresh(client)
        assert data['status'] == 'CONNECTION_FAILED'
        assert 'private' not in str(data)


def test_market_health_is_independent_from_authentication():
    async def unavailable(*args, **kwargs):
        raise RuntimeError('secret details must not be displayed')
    env = {KEY_VARIABLE:'key', SECRET_VARIABLE:'secret'}
    with TestClient(create_app(environment=env, probe=lambda *args: {}, market_fetch=unavailable, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        for _ in range(100):
            data=client.get('/api/gate/status').json()
            if data['market_checked_at'] and data['checked_at']:
                break
            time.sleep(.01)
        assert data['status']=='CONNECTED_READ_ONLY'
        assert data['market_status']=='UNAVAILABLE'
        assert 'secret details' not in str(data)


def test_research_is_opt_in_and_remains_read_only():
    env={KEY_VARIABLE:'key', SECRET_VARIABLE:'secret', 'GATE_RESEARCH_ANALYSIS_ENABLED':'true'}
    calls=[]
    async def research(symbol,timeframe,equity):
        calls.append((symbol,timeframe,equity))
        return {'analysis_scope':'RESEARCH_ONLY','execution_enabled':False,
                'candle_closed_at':'2026-10-29T00:00:00Z','total_agents':300,
                'decision':{'candidate_action':'HOLD'}}
    with TestClient(create_app(environment=env, probe=lambda *args: {'total':'1000'},
                               market_fetch=fake_market, research=research, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        for _ in range(1200):
            data=client.get('/api/gate/status').json()
            if data['research_status']=='AVAILABLE':
                break
            time.sleep(.01)
        assert data['research_status']=='AVAILABLE'
        assert data['execution_enabled'] is False
        assert calls==[('BTCUSDT','15m',1000.)]
        assert '1000' not in str(data) and 'secret' not in str(data)
        assert client.post('/api/demo/execute').status_code == 404


def test_inventory_runs_in_read_only_scope_without_public_balances():
    env={KEY_VARIABLE:'key', SECRET_VARIABLE:'secret'}
    with TestClient(create_app(environment=env,probe=lambda *args:{'total':'1000'},
                               market_fetch=fake_market,inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        for _ in range(1200):
            data=client.get('/api/gate/status').json()
            if data['inventory_checked_at'] and data['position_config_status'] != 'WAITING':
                break
            time.sleep(.01)
        assert data['inventory_status']=='AVAILABLE' and data['execution_enabled'] is False
        assert data['position_config_status']=='ONE_WAY_ISOLATED_1X'
        assert 'positions' not in data and 'secret' not in str(data)

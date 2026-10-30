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
    with TestClient(create_app(environment=env, probe=lambda *args: {'total': '123456789.123456789'}, market_fetch=fake_market, inventory_fn=fake_inventory, position_config_fn=fake_position_config)) as client:
        data = status_after_refresh(client)
        assert data['status'] == 'CONNECTED_READ_ONLY'
        assert data['execution_enabled'] is False
        assert 'private' not in str(data) and '123456789.123456789' not in str(data)
        assert not {'account', 'balance', 'total', 'equity', 'available'} & set(data)


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


def test_research_is_opt_in_and_remains_read_only(tmp_path):
    env={KEY_VARIABLE:'key', SECRET_VARIABLE:'secret', 'GATE_RESEARCH_ANALYSIS_ENABLED':'true',
         'RAILWAY_GIT_COMMIT_SHA':'abcdef123456'}
    calls=[]
    async def research(symbol,timeframe,equity):
        calls.append((symbol,timeframe,equity))
        return {'analysis_scope':'RESEARCH_ONLY','execution_enabled':False, 'exchange':'GATE',
                'market':'GATE_USDT_FUTURES_TESTNET','source':'AGENTS','symbol':symbol,'timeframe':timeframe,
                'reference_price':80000,'reference_price_type':'LAST_CLOSED_CANDLE',
                'candle_closed_at':'2026-10-29T00:00:00Z','total_agents':300,
                'decision':{'candidate_action':'BLOCK'},
                'agents':[{'agent_name':f'Agent{i}', 'status':'COMPLETED', 'signal':'WAIT', 'confidence':0} for i in range(300)]}
    with TestClient(create_app(environment=env, probe=lambda *args: {'total':'1000'},
                               market_fetch=fake_market, research=research, inventory_fn=fake_inventory, position_config_fn=fake_position_config,
                               storage_path=str(tmp_path),mount_check=lambda _:True)) as client:
        for _ in range(1200):
            data=client.get('/api/gate/status').json()
            if data['research_status']=='AVAILABLE':
                break
            time.sleep(.01)
        assert data['research_status']=='AVAILABLE'
        assert data['storage_status']=='AVAILABLE' and data['recovery_status']=='CLEAR'
        assert data['execution_enabled'] is False
        assert calls==[('BTCUSDT','15m',1000.)]
        assert '1000' not in str(data) and 'secret' not in str(data)
        assert client.post('/api/demo/execute').status_code == 404
    import sqlite3
    with sqlite3.connect(tmp_path/'capital-cipher-gate-testnet'/'research.sqlite3') as db:
        assert db.execute('SELECT action,COUNT(*) FROM research').fetchone()==('BLOCK',1)


def test_missing_mount_is_reported_and_never_looks_ready(tmp_path):
    with TestClient(create_app(environment={}, market_fetch=fake_market, inventory_fn=fake_inventory,
                               position_config_fn=fake_position_config,storage_path=str(tmp_path),
                               mount_check=lambda _:False)) as client:
        data=client.get('/api/gate/status').json()
        assert data['storage_status']=='UNAVAILABLE' and data['recovery_status']=='UNAVAILABLE'
        assert data['execution_enabled'] is False
    assert not list(tmp_path.iterdir())


def test_unresolved_cycle_survives_restart_and_is_reported(tmp_path):
    from app.demo.gate_cycle_journal import GateCycleJournal
    directory=tmp_path/'capital-cipher-gate-testnet'
    directory.mkdir()
    journal=GateCycleJournal(str(directory/'cycles.sqlite3'))
    journal.create('cycle_12345','BTC_USDT','79000',2)
    journal.record_entry_intent('cycle_12345')
    journal.close()
    with TestClient(create_app(environment={},market_fetch=fake_market,inventory_fn=fake_inventory,
                               position_config_fn=fake_position_config,storage_path=str(tmp_path),
                               mount_check=lambda _:True)) as client:
        data=client.get('/api/gate/status').json()
        assert data['storage_status']=='AVAILABLE' and data['recovery_status']=='REQUIRED'
        assert data['execution_enabled'] is False
        assert 'cycle_12345' not in str(data)


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



def test_requested_oms_connection_failure_is_not_reported_healthy(tmp_path):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def fail(*args,**kwargs):
        raise RuntimeError('private connection material')
        yield
    with TestClient(create_app(environment={'GATE_OMS_CONNECTION_ENABLED':'true'},
            probe=lambda *args:{},market_fetch=fake_market,inventory_fn=fake_inventory,
            position_config_fn=fake_position_config,storage_path=str(tmp_path),mount_check=lambda _:True,
            oms_connector=fail)) as client:
        assert client.get('/health').status_code==503
        state=client.get('/api/gate/status').json()
        assert state['oms_connection_status']=='CONNECTION_FAILED'
        assert state['execution_enabled'] is False and 'private' not in str(state)


def test_connected_oms_status_never_implies_writes_are_enabled(tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    @asynccontextmanager
    async def connected(values,journal,**kwargs):
        async def run(stop): await stop.wait()
        assert journal is not None and kwargs['mount_verified'] is True
        yield SimpleNamespace(status='CONNECTED_READ_ONLY',writes_enabled=False,run=run)
    with TestClient(create_app(environment={'GATE_OMS_CONNECTION_ENABLED':'true'},
            probe=lambda *args:{},market_fetch=fake_market,inventory_fn=fake_inventory,
            position_config_fn=fake_position_config,storage_path=str(tmp_path),mount_check=lambda _:True,
            oms_connector=connected)) as client:
        assert client.get('/health').status_code==200
        state=client.get('/api/gate/status').json()
        assert state['oms_connection_status']=='CONNECTED' and state['execution_enabled'] is False

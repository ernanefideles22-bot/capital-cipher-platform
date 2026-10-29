import time

from fastapi.testclient import TestClient

from app.demo.gate_service import KEY_VARIABLE, SECRET_VARIABLE, create_app
from app.demo.gate_readonly import GateProbeError


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
    with TestClient(create_app(environment={}, probe=probe)) as client:
        assert status_after_refresh(client)['status'] == 'WAITING_FOR_CREDENTIALS'
        assert client.post('/api/demo/execute').status_code == 404


def test_success_exposes_status_only():
    env = {KEY_VARIABLE: 'private-key', SECRET_VARIABLE: 'private-secret'}
    with TestClient(create_app(environment=env, probe=lambda *args: {'total': '123'})) as client:
        data = status_after_refresh(client)
        assert data['status'] == 'CONNECTED_READ_ONLY'
        assert data['execution_enabled'] is False
        assert 'private' not in str(data) and '123' not in str(data)


def test_failure_never_leaks_credentials():
    def probe(*args):
        raise GateProbeError('private-secret')
    env = {KEY_VARIABLE: 'private-key', SECRET_VARIABLE: 'private-secret'}
    with TestClient(create_app(environment=env, probe=probe)) as client:
        data = status_after_refresh(client)
        assert data['status'] == 'CONNECTION_FAILED'
        assert 'private' not in str(data)

"""Public candle -> S2 -> isolated durable S3, without deployment or trading."""
import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.demo.gate_s3_collector import GateS3Collector
from app.demo.gate_service import create_app, _shadow_delay
from app.demo.gate_stage1_shadow import professional_shadow_snapshot
from app.strategy.gate_s2e_live_shadow import evaluate_shared_shadow
from app.strategy.gate_s3_durable_research import DurableS3ForwardResearch
from app.tests.test_gate_s2e_shared_shadow import same_observation, NOW
from app.tests.test_gate_s3_durable_research import candle, shadow

FLAGS = {'GATE_S1_SHADOW_ENABLED': 'true', 'GATE_S2E_SHADOW_ENABLED': 'true',
         'GATE_S3_SHADOW_ENABLED': 'true'}


async def fake_market(*args, **kwargs):
    return [{}]*60


async def park_after_one_cycle(_seconds):
    await asyncio.Event().wait()


def wait_http(client, predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = client.get('/api/gate/status').json()
        if predicate(result):
            return result
        time.sleep(.005)
    raise AssertionError(result)


def source_runner(*, max_cycles=1):
    counters = {}
    async def runner(symbol, *, strategy_shadow_hook, observation_sink=None):
        index = counters.get(symbol, 0)
        counters[symbol] = index+1
        bar = candle(index, symbol=symbol, high=105. if index == 3 else 101.)
        extension = shadow(bar, action='LONG' if index == 0 else 'WAIT')
        if observation_sink is not None:
            observation_sink(bar, extension, observed_at=bar.received_at)
        return {'symbol': symbol, 'status': 'READ_ONLY_CHART_READY',
                'agent_signal': 'NEUTRAL', 'execution_authorized': False,
                'source_decision_cohort_modified': False, 'strategy_shadow': extension}
    pauses = 0
    async def pause(seconds):
        nonlocal pauses
        pauses += 1
        if pauses >= max_cycles:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(.015)
    return runner, pause


@pytest.mark.parametrize('enabled', [(), ('GATE_S3_SHADOW_ENABLED',),
    ('GATE_S1_SHADOW_ENABLED',), ('GATE_S2E_SHADOW_ENABLED',),
    ('GATE_S1_SHADOW_ENABLED', 'GATE_S2E_SHADOW_ENABLED'),
    ('GATE_S1_SHADOW_ENABLED', 'GATE_S3_SHADOW_ENABLED'),
    ('GATE_S2E_SHADOW_ENABLED', 'GATE_S3_SHADOW_ENABLED')])
def test_three_explicit_flags_are_required_before_any_s3_file_or_worker(tmp_path, enabled):
    def forbidden(*args, **kwargs):
        raise AssertionError('S3 factory must not run')
    runner, _ = source_runner()
    async def compatible(symbol, **kwargs):
        if 'strategy_shadow_hook' not in kwargs:
            return {'symbol': symbol, 'status': 'READ_ONLY_CHART_READY',
                    'agent_signal': 'NEUTRAL', 'execution_authorized': False,
                    'source_decision_cohort_modified': False}
        assert 'observation_sink' not in kwargs
        return await runner(symbol, **kwargs)
    app = create_app(environment={k: 'true' for k in enabled}, storage_path=str(tmp_path),
                     mount_check=lambda _: True, market_fetch=fake_market,
                     stage1_shadow_runner=compatible, forward_research_factory=forbidden)
    with TestClient(app) as client:
        info = client.get('/api/gate/research/forward').json()
        assert info['collection']['status'] == 'DISABLED'
        assert info['comparison'] is None
        assert not client.get('/api/gate/status').json()['execution_enabled']
    assert not (tmp_path/'capital-cipher-gate-s3').exists()


@pytest.mark.asyncio
async def test_actual_public_snapshot_hook_adds_no_fetch_and_copies_its_inputs():
    calls, observed = [], []
    async def fetch(symbol, tf, *, limit):
        calls.append((symbol, tf, limit))
        return same_observation(tf)
    def capture(bar, ext, *, observed_at):
        observed.append((bar, ext, observed_at))
        ext['status'] = 'MUTATED_BY_BAD_SINK'
        raise RuntimeError('PRIVATE-SECRET-NEVER-SHOW')
    from datetime import timedelta
    report = await professional_shadow_snapshot(
        'BTCUSDT', fetch=fetch, observation_clock=lambda: NOW+timedelta(seconds=3),
        strategy_shadow_hook=evaluate_shared_shadow, observation_sink=capture)
    assert calls == [('BTCUSDT', tf, 200) for tf in ('15m', '1h', '4h')]
    assert observed[0][0].timeframe == '15m'
    assert observed[0][0].received_at == observed[0][2]
    assert report['status'] == 'READ_ONLY_CHART_READY'
    assert report['strategy_shadow']['status'] == 'READ_ONLY_CONFLUENCE_READY'
    assert report['forward_research']['status'] == 'BLOCKED'
    assert 'PRIVATE-SECRET' not in str(report)


def test_service_captures_all_markets_and_recovers_open_positions(tmp_path):
    runner, pause = source_runner(max_cycles=3)
    def app():
        return create_app(environment=FLAGS, storage_path=str(tmp_path), mount_check=lambda _: True,
                          market_fetch=fake_market, stage1_shadow_runner=runner, stage1_sleep=pause)
    with TestClient(app()) as client:
        status = wait_http(client, lambda s: s['forward_research'].get('journal_observations') == 9)
        before = client.get('/api/gate/research/forward').json()
        assert status['professional_shadow']['status'] == 'AVAILABLE'
        assert status['strategy_shadow']['status'] == 'AVAILABLE'
        assert not status['execution_enabled']
        assert len(before['comparison']['comparisons']) == 9
        assert sum(p['baseline']['open_position'] for p in before['comparison']['comparisons']) == 3
        assert client.post('/api/demo/execute').status_code == 404
    # Each synthetic source resumes on the next candle; recovery is not a
    # fabricated receipt of missed historical bars.
    with TestClient(app()) as client:
        status = wait_http(client, lambda s: s['forward_research'].get('journal_observations') == 12)
        after = client.get('/api/gate/research/forward').json()
        assert sum(p['baseline']['completed_trades'] for p in after['comparison']['comparisons']) == 3
        assert not status['forward_research']['execution_authorized']


@pytest.mark.parametrize('mount_ok', [False, True])
def test_storage_startup_failure_does_not_block_s1_s2_or_leak_secrets(tmp_path, mount_ok, caplog):
    runner, _ = source_runner()
    calls = []
    def failed_factory(root, *, mount_verified):
        calls.append((root, mount_verified))
        raise RuntimeError('PRIVATE-CREDENTIAL-DO-NOT-EXPOSE')
    env = dict(FLAGS, CAPITAL_CIPHER_GATE_TESTNET_API_SECRET='PRIVATE-CREDENTIAL-DO-NOT-EXPOSE')
    app = create_app(environment=env, storage_path=str(tmp_path), mount_check=lambda _: mount_ok,
                     market_fetch=fake_market, stage1_shadow_runner=runner,
                     forward_research_factory=failed_factory)
    with TestClient(app) as client:
        status = wait_http(client, lambda s: s['professional_shadow']['checked_at'] is not None)
        assert status['professional_shadow']['status'] == 'AVAILABLE'
        assert status['strategy_shadow']['status'] == 'AVAILABLE'
        assert status['forward_research']['status'] == 'BLOCKED'
        assert client.get('/api/gate/research/forward').status_code == 503
        assert client.get('/health').status_code == 200
        assert 'PRIVATE-CREDENTIAL' not in str(status)+caplog.text
    assert bool(calls) is mount_ok


def test_slow_disk_does_not_hold_the_event_loop_or_original_market_monitor(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowJournal(DurableS3ForwardResearch):
        def record(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().record(*args, **kwargs)
    def factory(root, *, mount_verified):
        return GateS3Collector(root, mount_verified=mount_verified, journal_factory=SlowJournal)
    runner, pause = source_runner(max_cycles=3)
    app = create_app(environment=FLAGS, storage_path=str(tmp_path), mount_check=lambda _: True,
                     market_fetch=fake_market, stage1_shadow_runner=runner, stage1_sleep=pause,
                     forward_research_factory=factory)
    with TestClient(app) as client:
        try:
            assert entered.wait(2)
            status = wait_http(client, lambda s: s['professional_shadow']['cycle_count'] == 3)
            assert status['professional_shadow']['status'] == 'AVAILABLE'
            assert status['strategy_shadow']['status'] == 'AVAILABLE'
            assert status['forward_research']['journal_observations'] == 0
            assert status['forward_research']['pending_queue_size'] > 0
            assert client.get('/health').status_code == 200
        finally:
            release.set()
        wait_http(client, lambda s: s['forward_research'].get('journal_observations') == 9)


def test_record_failure_keeps_s1_s2_healthy_and_financial_endpoint_blocked(tmp_path, caplog):
    class BrokenJournal(DurableS3ForwardResearch):
        def record(self, *args, **kwargs):
            raise OSError('PRIVATE-DISK-DETAIL-NEVER-EXPOSE')
    def factory(root, *, mount_verified):
        return GateS3Collector(root, mount_verified=mount_verified, journal_factory=BrokenJournal)
    runner, _ = source_runner()
    app = create_app(environment=FLAGS, storage_path=str(tmp_path), mount_check=lambda _: True,
                     market_fetch=fake_market, stage1_shadow_runner=runner,
                     forward_research_factory=factory)
    with TestClient(app) as client:
        result = wait_http(client, lambda s: s['forward_research']['status'] == 'BLOCKED'
                           and s['professional_shadow']['checked_at'] is not None)
        assert result['professional_shadow']['status'] == 'AVAILABLE'
        assert result['strategy_shadow']['status'] == 'AVAILABLE'
        response = client.get('/api/gate/research/forward')
        assert response.status_code == 503 and response.json()['comparison'] is None
        assert client.get('/health').status_code == 200
        assert not result['execution_enabled']
        assert 'PRIVATE-DISK-DETAIL' not in str(result)+caplog.text


def test_real_s1_s2_to_service_journal_uses_only_the_original_nine_series(tmp_path):
    from datetime import timedelta
    calls = []
    async def fetch(symbol, tf, *, limit):
        calls.append((symbol, tf, limit))
        return same_observation(tf)
    async def real_runner(symbol, **kwargs):
        return await professional_shadow_snapshot(
            symbol, fetch=fetch, observation_clock=lambda: NOW+timedelta(seconds=3), **kwargs)
    app = create_app(environment=FLAGS, storage_path=str(tmp_path), mount_check=lambda _: True,
                     market_fetch=fake_market, stage1_shadow_runner=real_runner,
                     stage1_sleep=park_after_one_cycle)
    with TestClient(app) as client:
        result = wait_http(client, lambda s: s['forward_research'].get('journal_observations') == 3)
        report = client.get('/api/gate/research/forward').json()['comparison']
        assert result['professional_shadow']['status'] == 'AVAILABLE'
        assert result['strategy_shadow']['status'] == 'AVAILABLE'
        assert len(calls) == 9 and all(limit == 200 for _, _, limit in calls)
        # These source fixtures predate the frozen cutoff: storing them cannot
        # transform historical warm-up into eligible forward PnL.
        assert report['source_observations'] == 3
        assert report['comparisons'] == []
        assert not report['profitability_proven']


@pytest.mark.parametrize('phase,expected', [(2, 8), (10, 900), (14, 896), (899, 11)])
def test_only_s3_optin_anchors_sampling_to_future_confirmed_15m_boundaries(phase, expected):
    assert _shadow_delay(True, 9000+phase) == expected
    assert _shadow_delay(False, 9000+phase) == 900

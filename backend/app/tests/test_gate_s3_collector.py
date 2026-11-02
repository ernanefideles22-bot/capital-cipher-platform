"""Isolated queue, storage failures, cold recovery and immutable observations."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.demo.gate_s3_collector import GateS3Collector, JOURNAL_NAME, QUEUE_CAPACITY
from app.strategy.gate_s3_durable_research import DurableS3ForwardResearch
from app.tests.test_gate_s3_durable_research import candle, shadow, SELECTED


def wait_state(collector, predicate, timeout=3):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = collector.snapshot()
        if predicate(result):
            return result
        time.sleep(.005)
    raise AssertionError(collector.snapshot())


@contextmanager
def running(root, **kwargs):
    collector = GateS3Collector(root, mount_verified=True, **kwargs)
    collector.start()
    try:
        wait_state(collector, lambda s: s['collection']['status'] != 'STARTING')
        yield collector
    finally:
        collector.stop()


def capture(collector, index, **kwargs):
    bar_kwargs = {k: kwargs.pop(k) for k in tuple(kwargs)
                  if k in ('high', 'low', 'open_price', 'symbol')}
    bar = candle(index, **bar_kwargs)
    return collector.capture(bar, shadow(bar, **kwargs), observed_at=bar.received_at)


def count(collector, n):
    return wait_state(collector, lambda s: s['collection']['journal_observations'] == n)


def selected(report, arm='STANDALONE'):
    pair = next(p for p in report['comparison']['comparisons'] if p['strategy'] == SELECTED)
    return pair['baseline' if arm == 'STANDALONE' else 'confluence']


def test_restart_preserves_open_position_then_closes_it_with_costs(tmp_path):
    legacy = tmp_path/'legacy.sqlite3'
    legacy.write_bytes(b'untouched legacy data')
    with running(tmp_path) as collector:
        capture(collector, 0, action='LONG', filtered='WAIT')
        capture(collector, 1)
        capture(collector, 2)
        before = count(collector, 3)
        assert selected(before)['open_position']
        assert not selected(before, 'S1_CONFLUENCE')['open_position']
        assert not before['execution_authorized']
    with running(tmp_path) as restored:
        recovered = count(restored, 3)
        assert recovered['comparison'] == before['comparison']
        capture(restored, 3, high=105.)
        after = count(restored, 4)
        assert selected(after)['completed_trades'] == 1
        assert selected(after)['realized_net_pnl'] > 0
        assert not selected(after)['open_position']
        assert after['comparison']['valuation']['friction_assumptions']['fee_bps_side'] == 6
    assert legacy.read_bytes() == b'untouched legacy data'


def test_later_same_candle_keeps_original_receipt_and_decision_after_restart(tmp_path):
    with running(tmp_path) as collector:
        capture(collector, 0, action='LONG')
        before = count(collector, 1)
    with running(tmp_path) as restored:
        later = candle(0).model_copy(update={'received_at': candle(0).received_at+timedelta(seconds=20)})
        restored.capture(later, shadow(later, action='SHORT'), observed_at=later.received_at)
        result = wait_state(restored, lambda s: s['collection']['duplicate_candles_skipped'] == 1)
        assert result['comparison'] == before['comparison']
        assert result['collection']['journal_observations'] == 1
    path = tmp_path/'capital-cipher-gate-s3'/JOURNAL_NAME
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute('SELECT payload_json FROM s3_observations').fetchone()[0])
    assert payload['observed_at'] == candle(0).received_at.isoformat()
    assert any(h['proposed_action'] == 'LONG' for h in payload['shadow']['hypotheses'])


@pytest.mark.parametrize('kind', ['changed', 'gap', 'backwards', 'executable'])
def test_invalid_followup_blocks_without_reset_or_repair(tmp_path, kind):
    with running(tmp_path) as collector:
        capture(collector, 0)
        count(collector, 1)
        if kind == 'changed':
            capture(collector, 0, high=102.)
        elif kind == 'gap':
            capture(collector, 2)
        elif kind == 'backwards':
            capture(collector, -1)
        else:
            capture(collector, 1, extra={'execution_authorized': True})
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['collection']['journal_observations'] == 1
        assert result['comparison'] is None
        assert capture(collector, 1)['status'] == 'BLOCKED'
    path = tmp_path/'capital-cipher-gate-s3'/JOURNAL_NAME
    with DurableS3ForwardResearch(path) as preserved:
        assert preserved.status()['observations'] == 1


def test_disk_failure_is_sanitized_and_never_reported_as_a_commit(tmp_path, caplog):
    class BrokenJournal(DurableS3ForwardResearch):
        def record(self, *args, **kwargs):
            raise sqlite3.OperationalError('PRIVATE-KEY-NEVER-EXPOSE')
    with running(tmp_path, journal_factory=BrokenJournal) as collector:
        capture(collector, 0)
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['collection']['journal_observations'] == 0
        assert result['comparison'] is None
        assert 'PRIVATE-KEY' not in str(result)+caplog.text


def test_queue_overflow_halts_instead_of_silently_dropping_candles(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def slow_factory(path):
        entered.set()
        assert release.wait(3)
        return DurableS3ForwardResearch(path)
    collector = GateS3Collector(tmp_path, mount_verified=True, journal_factory=slow_factory)
    collector.start()
    try:
        assert entered.wait(2)
        for i in range(QUEUE_CAPACITY):
            assert capture(collector, i)['status'] == 'QUEUED_NOT_COMMITTED'
        assert capture(collector, QUEUE_CAPACITY)['status'] == 'BLOCKED'
        result = collector.snapshot()
        assert result['collection']['error_code'] == 'QUEUE_OVERFLOW'
        assert result['collection']['journal_observations'] == 0
        assert result['comparison'] is None
    finally:
        release.set()
        collector.stop()


def test_second_writer_cannot_share_the_research_journal(tmp_path):
    with running(tmp_path) as first:
        with running(tmp_path) as second:
            assert second.snapshot()['collection']['status'] == 'BLOCKED'
            capture(first, 0)
            assert count(first, 1)['collection']['status'] == 'AVAILABLE'


@pytest.mark.parametrize('kind', ['unverified', 'directory_symlink', 'file_symlink', 'unrelated_db', 'corrupt_head'])
def test_unsafe_or_corrupt_storage_is_never_replaced(tmp_path, kind):
    directory = tmp_path/'capital-cipher-gate-s3'
    if kind == 'directory_symlink':
        target = tmp_path/'outside'
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
    elif kind != 'unverified':
        directory.mkdir()
        path = directory/JOURNAL_NAME
        if kind == 'file_symlink':
            target = tmp_path/'important-file'
            target.write_text('preserve me')
            path.symlink_to(target)
        elif kind == 'unrelated_db':
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE unrelated(value TEXT)')
                db.execute("INSERT INTO unrelated VALUES ('preserve me')")
        else:
            with DurableS3ForwardResearch(path) as journal:
                bar = candle(0)
                journal.record(bar, shadow(bar), observed_at=bar.received_at)
            with sqlite3.connect(path) as db:
                db.execute('UPDATE s3_heads SET event_count=77')
    collector = GateS3Collector(tmp_path, mount_verified=kind != 'unverified')
    collector.start()
    try:
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['comparison'] is None
    finally:
        collector.stop()
    if kind == 'unverified':
        assert not directory.exists()
    elif kind == 'directory_symlink':
        assert list(target.iterdir()) == []
    elif kind == 'file_symlink':
        assert target.read_text() == 'preserve me'
    elif kind == 'unrelated_db':
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT * FROM unrelated').fetchall() == [('preserve me',)]
            assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='s3_schema'").fetchone()[0] == 0
    else:
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT event_count FROM s3_heads').fetchone()[0] == 77


def test_shutdown_reports_inflight_io_then_closes_without_accepting_new_data(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class SlowJournal(DurableS3ForwardResearch):
        def record(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().record(*args, **kwargs)
    with running(tmp_path, journal_factory=SlowJournal) as collector:
        capture(collector, 0)
        assert entered.wait(2)
        collector.stop(timeout=.01)
        assert collector.snapshot()['collection']['shutdown_pending']
        assert capture(collector, 1)['status'] == 'BLOCKED'
        release.set()
        result = wait_state(collector, lambda s: not s['collection']['shutdown_pending'])
        assert result['collection']['journal_observations'] == 1


def test_capacity_limit_preserves_all_commits_and_requires_review(tmp_path, monkeypatch):
    monkeypatch.setattr('app.demo.gate_s3_collector.MAX_COLLECTOR_OBSERVATIONS', 2)
    with running(tmp_path) as collector:
        capture(collector, 0)
        capture(collector, 1)
        count(collector, 2)
        capture(collector, 2)
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['collection']['error_code'] == 'OBSERVATION_LIMIT_REACHED'
        assert result['collection']['journal_observations'] == 2
    with running(tmp_path) as restored:
        assert restored.snapshot()['collection']['status'] == 'BLOCKED'
        assert restored.snapshot()['collection']['journal_observations'] == 2


def test_disk_reserve_blocks_new_writes_without_touching_previous_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    with running(tmp_path) as collector:
        capture(collector, 0)
        count(collector, 1)
        monkeypatch.setattr('app.demo.gate_s3_collector.shutil.disk_usage',
                            lambda _: SimpleNamespace(free=0))
        capture(collector, 1)
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['collection']['error_code'] == 'LOW_DISK_SPACE'
        assert result['collection']['journal_observations'] == 1
    path = tmp_path/'capital-cipher-gate-s3'/JOURNAL_NAME
    with DurableS3ForwardResearch(path) as journal:
        assert journal.status()['observations'] == 1


def test_report_failure_after_commit_preserves_data_but_hides_stale_financials(tmp_path, monkeypatch):
    import app.demo.gate_s3_collector as module
    original = module.read_only_comparison_report
    calls = 0
    def failed_after_commit(path):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError('PRIVATE-ERROR-DO-NOT-EXPOSE')
        return original(path)
    monkeypatch.setattr(module, 'read_only_comparison_report', failed_after_commit)
    with running(tmp_path) as collector:
        capture(collector, 0)
        result = wait_state(collector, lambda s: s['collection']['status'] == 'BLOCKED')
        assert result['collection']['journal_observations'] == 1
        assert result['comparison'] is None
        assert result['collection']['error_code'] == 'REPORT_VERIFICATION_FAILURE'
        assert 'PRIVATE-ERROR' not in str(result)
    monkeypatch.setattr(module, 'read_only_comparison_report', original)
    with running(tmp_path) as restored:
        assert count(restored, 1)['comparison']['source_observations'] == 1


def test_analysis_cutoff_and_later_decision_availability_survive_cold_replay(tmp_path):
    bar = candle(0)
    ready = bar.received_at+timedelta(milliseconds=250)
    extension = shadow(bar, action='LONG')
    extension['analysis_cutoff_utc'] = extension['as_of_utc']
    extension['as_of_utc'] = ready.isoformat()
    with running(tmp_path) as collector:
        collector.capture(bar, extension, observed_at=ready)
        before = count(collector, 1)
        assert before['collection']['source_last_observed_at_utc'] == ready.isoformat()
    with running(tmp_path) as restored:
        assert count(restored, 1)['comparison'] == before['comparison']
    path = tmp_path/'capital-cipher-gate-s3'/JOURNAL_NAME
    with sqlite3.connect(path) as db:
        payload = json.loads(db.execute('SELECT payload_json FROM s3_observations').fetchone()[0])
    assert payload['shadow']['analysis_cutoff_utc'] == bar.received_at.isoformat()
    assert payload['shadow']['as_of_utc'] == payload['observed_at'] == ready.isoformat()


@pytest.mark.parametrize('offset', [-1, 1])
def test_impossible_analysis_cutoff_is_not_persisted(tmp_path, offset):
    bar = candle(0)
    extension = shadow(bar)
    extension['analysis_cutoff_utc'] = (bar.received_at+timedelta(seconds=offset)).isoformat()
    with running(tmp_path) as collector:
        result = collector.capture(bar, extension, observed_at=bar.received_at)
        assert result['status'] == 'BLOCKED'
        assert collector.snapshot()['collection']['journal_observations'] == 0

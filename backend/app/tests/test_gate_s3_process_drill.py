"""Real isolated SIGKILL/restart boundaries and independent financial oracle."""
import copy
import json
import sqlite3
from contextlib import closing

import pytest

pytest.importorskip('resource')
from scripts.gate_s3_isolated_drill import (
    PHASES, SELECTED, child_environment, crash_drill, journal_path,
    oracle, percentiles, scale_drill, seed,
)
from app.strategy.gate_s3_paired_evidence import read_only_comparison_report


@pytest.mark.parametrize('phase', PHASES)
def test_real_sigkill_retains_exact_committed_prefix_and_reconciles_after_restart(tmp_path, phase):
    result = crash_drill(tmp_path/phase, phase)
    assert result['exit_code'] == -9 and result['signal'] == 'SIGKILL'
    expected = 10 if phase == 'after_commit_before_memory' else 9
    assert result['visible_committed_before_kill'] == expected
    assert result['published_before_kill'] == 9
    if phase == 'before_commit':
        assert result['wal_bytes_at_barrier'] > 32  # More than an empty WAL header.
    assert result['final_observations'] == 12
    assert result['duplicate_retries_skipped'] == 1
    assert result['financial_oracle_max_error'] <= 1e-8
    assert result['exact_report_matches_reference']
    assert result['recovered_prefix_matches_disk']
    # Independent known answers for the fixed synthetic fixture, including
    # component costs (net agreement alone could hide offsetting mistakes).
    with closing(sqlite3.connect(journal_path(tmp_path/phase))) as db:
        rows = db.execute('''SELECT symbol,outcome_json FROM s3_observations
            WHERE seq IN (SELECT last_seq FROM s3_heads)''').fetchall()
    for symbol, raw in rows:
        for account in json.loads(raw)['accounts']:
            active = account['strategy'] == SELECTED and not (
                symbol == 'ETHUSDT' and account['arm'] == 'S1_CONFLUENCE')
            expected = {'gross_after_slippage_before_fees': 0., 'fees': 0.,
                        'funding': 0., 'net_pnl': 0.}
            if active and symbol == 'SOLUSDT':
                expected = {'gross_after_slippage_before_fees': -2.576455,
                            'fees': .15145587, 'funding': .0124925, 'net_pnl': -2.74040337}
            elif active:
                expected = {'gross_after_slippage_before_fees': 4.921955,
                            'fees': .15304317, 'funding': .0125075, 'net_pnl': 4.75640433}
            for field, value in expected.items():
                assert account[field] == pytest.approx(value, abs=1e-8, rel=0)


def test_independent_ledger_oracle_detects_wrong_pnl_instead_of_only_comparing_replays(tmp_path):
    root = tmp_path/'oracle'
    root.mkdir()
    seed(root, 12)
    report = read_only_comparison_report(journal_path(root))
    assert oracle(report, closed=True) <= 1e-8
    bad = copy.deepcopy(report)
    selected = next(p for p in bad['comparisons'] if p['strategy'] == SELECTED)
    selected['baseline']['realized_net_pnl'] += 1.
    with pytest.raises(AssertionError):
        oracle(bad, closed=True)


def test_scale_smoke_uses_real_collector_and_cached_asgi_endpoint(tmp_path):
    result = scale_drill(tmp_path/'scale', 30, 3)
    assert result['status'] == 'MEASURED'
    assert result['history_observations'] == 30
    assert result['appended_observations'] == 3
    for metric in ('enqueue_ms', 'append_to_verified_cache_ms', 'cached_asgi_http_ms'):
        assert result[metric]['samples'] == 3
        assert result[metric]['p99'] >= result[metric]['p50'] >= 0
    assert result['peak_rss_mib'] >= result['initial_rss_mib'] > 0
    assert result['journal_storage_total_bytes'] == sum(result['journal_storage_bytes'].values())
    assert not result['execution_authorized']


def test_percentiles_are_descriptive_nearest_rank_with_explicit_small_sample_count():
    assert percentiles(list(range(1, 21))) == {'samples': 20, 'p50': 10, 'p95': 19, 'p99': 20}


def test_child_process_environment_does_not_forward_parent_exchange_credentials(monkeypatch):
    monkeypatch.setenv('CAPITAL_CIPHER_GATE_TESTNET_API_SECRET', 'PRIVATE-DO-NOT-FORWARD')
    monkeypatch.setenv('GATE_OMS_WRITES_ENABLED', 'true')
    env = child_environment()
    assert 'PRIVATE-DO-NOT-FORWARD' not in str(env)
    assert env['GATE_OMS_WRITES_ENABLED'] == 'false'

"""Local-only S3 SIGKILL, independent ledger reconciliation and scale drill.

All data are deterministic synthetic fixtures in TemporaryDirectory. No exchange,
network broker, Railway, existing journal or external account is used. Larger
fixtures are bulk-built for setup only; measurements use the real collector/API.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import resource
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.demo.gate_s3_collector import GateS3Collector, JOURNAL_NAME
from app.strategy.gate_classical_s2 import STRATEGIES
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_s2e_live_shadow import S2E_POLICY, S2E_POLICY_COMMIT
from app.strategy.gate_s3_durable_research import (
    DurableS3ForwardResearch, GENESIS_HASH, _canonical, _event_hash, _serialize,
)
from app.strategy.gate_s3_forward_scorecard import ShadowForwardScorecard
from app.strategy.gate_s3_paired_evidence import read_only_comparison_report

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
SELECTED = 'DONCHIAN_VOLUME_BREAKOUT_V1'
T0 = datetime(2026, 11, 2, 0, 15, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve()
PHASES = ('open_position', 'before_commit', 'after_commit_before_memory')
PROVISIONAL_BUDGETS = {
    'enqueue_p99_ms': 50., 'cached_asgi_http_p99_ms': 200.,
    'fresh_process_api_ready_ms': 60000., 'append_to_verified_cache_p99_ms': 30000.,
    'peak_rss_mib': 256.,
}


def packet(index):
    symbol = SYMBOLS[index % 3]
    bar_index = index // 3
    closed = T0+timedelta(minutes=15*bar_index)
    phase = timedelta(milliseconds=50*(index % 3))
    receipt = closed+timedelta(seconds=5)+phase
    cutoff = closed+timedelta(seconds=6)+phase
    ready = closed+timedelta(seconds=7)+phase
    bar = Candle(exchange=Exchange.GATE, symbol=symbol, timeframe='15m',
                 open=100., high=105. if bar_index == 3 else 101.,
                 low=99., close=100., volume=100., closed_at=closed, received_at=receipt)
    rows = []
    for name in sorted(STRATEGIES):
        action = ('SHORT' if symbol == 'SOLUSDT' else 'LONG') if name == SELECTED and bar_index == 0 else 'WAIT'
        filtered = 'WAIT' if symbol == 'ETHUSDT' else action
        rows.append({
            'strategy_id': name, 'strategy_version': STRATEGIES[name].version,
            'timeframe': '15m', 'proposed_action': action, 'filtered_action': filtered,
            'stop_distance': 2. if action != 'WAIT' else None,
            'target_distance': 4. if action != 'WAIT' else None,
            'rejection_reasons': ['HIGHER_TIMEFRAMES_NOT_ALIGNED']
                if action != 'WAIT' and filtered == 'WAIT' else [],
            'execution_authorized': False, 'predictive_edge_proven': False,
        })
    shadow = {
        'status': 'READ_ONLY_CONFLUENCE_READY', 'symbol': symbol,
        'as_of_utc': ready.isoformat(), 'analysis_cutoff_utc': cutoff.isoformat(),
        'policy_id': S2E_POLICY, 'policy_commit': S2E_POLICY_COMMIT,
        'source_decision_cohort_modified': False, 'oms_touched': False,
        'execution_authorized': False, 'hypotheses': rows,
    }
    return bar, shadow, ready


def journal_path(root):
    return Path(root)/'capital-cipher-gate-s3'/JOURNAL_NAME


def seed(root, count, *, bulk=False):
    directory = journal_path(root).parent
    directory.mkdir(parents=True, mode=0o700)
    path = journal_path(root)
    if not bulk:
        with DurableS3ForwardResearch(path) as journal:
            for i in range(count):
                bar, shadow, clock = packet(i)
                journal.record(bar, shadow, observed_at=clock)
        return
    # Setup optimization ONLY: a subsequent real collector startup verifies
    # every hash, outcome and head before any performance sample is accepted.
    with DurableS3ForwardResearch(path):
        pass
    score = ShadowForwardScorecard()
    heads, counts = {}, {}
    with closing(sqlite3.connect(path)) as db:
        db.execute('BEGIN')
        for i in range(count):
            bar, shadow, clock = packet(i)
            payload = _serialize(bar, shadow, clock)
            outcome = _canonical(score.observe(bar, shadow, observed_at=clock))
            previous = heads.get(bar.symbol, (0, GENESIS_HASH))[1]
            digest = _event_hash(previous, payload, outcome)
            cur = db.execute('''INSERT INTO s3_observations(symbol,candle_closed_at,
                observed_at,payload_json,outcome_json,previous_hash,event_hash)
                VALUES (?,?,?,?,?,?,?)''', (bar.symbol, bar.closed_at.isoformat(),
                clock.isoformat(), payload, outcome, previous, digest))
            heads[bar.symbol] = (cur.lastrowid, digest)
            counts[bar.symbol] = counts.get(bar.symbol, 0)+1
        for symbol, (seq, digest) in heads.items():
            db.execute('INSERT INTO s3_heads VALUES (?,?,?,?)',
                       (symbol, seq, digest, counts[symbol]))
        db.commit()


def wait_collector(collector, expected, timeout=60):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = collector.snapshot()
        state = result['collection']
        if state['status'] == 'BLOCKED':
            raise AssertionError(state)
        if state['journal_observations'] == expected and result['comparison'] is not None:
            return result
        time.sleep(.005)
    raise TimeoutError(f'S3 did not publish {expected} committed observations')


def send(collector, index):
    bar, shadow, clock = packet(index)
    result = collector.capture(bar, shadow, observed_at=clock)
    if result['status'] != 'QUEUED_NOT_COMMITTED':
        raise AssertionError(result)


def oracle(report, *, closed):
    """Independent arithmetic: fixed fixture quantity/risk, no replay helpers."""
    worst_error = 0.
    for pair in report['comparisons']:
        for arm in ('baseline', 'confluence'):
            account = pair[arm]
            active = pair['strategy'] == SELECTED and not (
                pair['symbol'] == 'ETHUSDT' and arm == 'confluence')
            expected = 0.
            if active:
                direction = -1 if pair['symbol'] == 'SOLUSDT' else 1
                entry = 100.*(1+direction*.0006)
                raw_exit = entry+4 if direction == 1 else entry+2
                if not closed:
                    raw_exit = 100.
                exit_price = raw_exit*(1-direction*.0006)
                quantity = 1.25  # 1000 * .25% / stop distance 2; below notional cap.
                expected = ((exit_price-entry)*quantity*direction
                            -(entry+exit_price)*quantity*.0006
                            -entry*quantity*.0001)
            expected_realized = expected if closed else 0.
            for field, value in (
                ('realized_net_pnl', expected_realized),
                ('total_net_exit_estimate', expected),
                ('equity_including_open_exit_estimate', 1000.+expected),
            ):
                error = abs(account[field]-round(value, 8))
                worst_error = max(worst_error, error)
                if error > 1e-8:
                    raise AssertionError((pair['symbol'], arm, field, account[field], value))
            if account['completed_trades'] != int(active and closed):
                raise AssertionError('Unexpected completed trade count')
            if account['open_position'] != bool(active and not closed):
                raise AssertionError('Unexpected open-position state')
    if report['profitability_proven'] or report['execution_authorized']:
        raise AssertionError('Research gained trading authority')
    return worst_error


def child_environment():
    # Do not forward account/exchange credentials or the parent's app flags.
    return {'PATH': os.environ.get('PATH', ''), 'LANG': 'C.UTF-8',
            'PYTHONPATH': str(BACKEND), 'SYSTEM_MODE': 'PAPER',
            'OMS_TESTNET_ENABLED': 'false', 'GATE_OMS_CONNECTION_ENABLED': 'false',
            'GATE_OMS_WRITES_ENABLED': 'false'}


def fault_child(root, phase):
    holder = {}
    def barrier():
        state = holder['collector'].snapshot()['collection']
        print(json.dumps({'kind': 'BARRIER', 'phase': phase,
                          'published_count': state['journal_observations']}), flush=True)
        threading.Event().wait(30)
        raise TimeoutError('Parent did not terminate the isolated fault child')
    class ConnectionProxy:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, key):
            return getattr(self.connection, key)
        def execute(self, sql, *args, **kwargs):
            result = self.connection.execute(sql, *args, **kwargs)
            if phase == 'before_commit' and sql.lstrip().startswith('INSERT INTO s3_observations'):
                barrier()  # INSERT has executed, heads/COMMIT have not.
            if phase == 'after_commit_before_memory' and sql == 'COMMIT':
                barrier()  # Real COMMIT returned, scorer/cache still at prefix.
            return result
    class FaultJournal(DurableS3ForwardResearch):
        def __init__(self, path):
            super().__init__(path)
            self.db.execute('PRAGMA cache_size=1')  # Encourage uncommitted WAL spill.
            self.db = ConnectionProxy(self.db)
    collector = GateS3Collector(root, mount_verified=True, journal_factory=FaultJournal)
    holder['collector'] = collector
    collector.start()
    wait_collector(collector, 9, timeout=15)
    if phase == 'open_position':
        barrier()
    else:
        send(collector, 9)
        threading.Event().wait(30)
    raise TimeoutError('Fault child failed to reach its barrier')


def kill_at_barrier(root, phase):
    process = subprocess.Popen([sys.executable, str(SCRIPT), '--child-fault', phase,
                                '--root', str(root)], cwd=BACKEND, env=child_environment(),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b''
    barrier = None
    deadline = time.monotonic()+20
    try:
        while time.monotonic() < deadline and barrier is None:
            for key, _ in selector.select(.1):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    raise RuntimeError('Fault process exited before barrier')
                pending += data
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    message = json.loads(line)
                    if message.get('kind') == 'BARRIER':
                        barrier = message
                        break
        if barrier is None:
            raise TimeoutError('No process crash barrier')
        with closing(sqlite3.connect(journal_path(root).as_uri()+'?mode=ro', uri=True)) as db:
            visible = db.execute('SELECT count(*) FROM s3_observations').fetchone()[0]
        wal = Path(str(journal_path(root))+'-wal')
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        process.kill()  # SIGKILL only this child that the harness just created.
        process.communicate(timeout=5)
        if process.returncode != -9:
            raise AssertionError('Expected POSIX SIGKILL termination')
        expected = 10 if phase == 'after_commit_before_memory' else 9
        if visible != expected or barrier['published_count'] != 9:
            raise AssertionError((phase, visible, barrier))
        return {'phase': phase, 'signal': 'SIGKILL', 'exit_code': process.returncode,
                'visible_committed_before_kill': visible,
                'published_before_kill': barrier['published_count'], 'wal_bytes_at_barrier': wal_bytes}
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def crash_drill(root, phase):
    root = Path(root)
    root.mkdir()
    seed(root, 9)
    original = read_only_comparison_report(journal_path(root))
    oracle(original, closed=False)
    result = kill_at_barrier(root, phase)
    expected = result['visible_committed_before_kill']
    resumed = subprocess.run([sys.executable, str(SCRIPT), '--child-resume', str(expected),
                              '--root', str(root)], cwd=BACKEND, env=child_environment(),
                             capture_output=True, text=True, timeout=30, check=True)
    resumed = json.loads(resumed.stdout)
    final = resumed['comparison']
    result['fresh_process_recovery_ms'] = resumed['recovery_ms']
    error = oracle(final, closed=True)
    reference_root = root.parent/(root.name+'-reference')
    reference_root.mkdir()
    seed(reference_root, 12)
    reference = read_only_comparison_report(journal_path(reference_root))
    if final != reference:
        raise AssertionError('Interrupted and uninterrupted financial histories differ')
    result.update(final_observations=12, duplicate_retries_skipped=1,
                  financial_oracle_max_error=error, exact_report_matches_reference=True,
                  recovered_prefix_matches_disk=resumed['prefix_matches_disk'], passed=True)
    return result


def resume_child(root, expected):
    prefix = read_only_comparison_report(journal_path(root))
    collector = GateS3Collector(root, mount_verified=True)
    start = time.perf_counter()
    collector.start()
    try:
        recovered = wait_collector(collector, expected)
        recovery_ms = round((time.perf_counter()-start)*1000, 3)
        if recovered['comparison'] != prefix:
            raise AssertionError('Recovered cache differs from independently verified disk prefix')
        # A retried candle after restart must not become another trade/event.
        retry = 9 if expected == 10 else 8
        send(collector, retry)
        deadline = time.monotonic()+3
        while collector.snapshot()['collection']['duplicate_candles_skipped'] != 1:
            if time.monotonic() > deadline:
                raise TimeoutError('Duplicate was not coalesced')
            time.sleep(.005)
        for index in range(expected, 12):
            send(collector, index)
            wait_collector(collector, index+1)
        final = wait_collector(collector, 12)['comparison']
    finally:
        collector.stop()
    return {'recovery_ms': recovery_ms, 'prefix_matches_disk': True, 'comparison': final}


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {'samples': 0, 'p50': None, 'p95': None, 'p99': None}
    return {'samples': len(ordered), **{
        name: round(ordered[max(0, math.ceil(q*len(ordered))-1)], 3)
        for name, q in (('p50', .5), ('p95', .95), ('p99', .99))}}


def scale_child(root, count, samples):
    from fastapi.testclient import TestClient
    from app.demo.gate_service import create_app
    holder = {}
    async def source(symbol, *, strategy_shadow_hook, observation_sink):
        holder['collector'] = observation_sink.__self__
        return {'symbol': symbol, 'status': 'READ_ONLY_CHART_READY',
                'agent_signal': 'NEUTRAL', 'execution_authorized': False,
                'source_decision_cohort_modified': False, 'strategy_shadow': {
                    'status': 'READ_ONLY_CONFLUENCE_READY', 'execution_authorized': False,
                    'oms_touched': False, 'source_decision_cohort_modified': False}}
    async def parked(_):
        await asyncio.Event().wait()
    async def market(*args, **kwargs):
        return [{}]*60
    @asynccontextmanager
    async def no_oms(*args, **kwargs):
        yield None
    env = {'GATE_S1_SHADOW_ENABLED': 'true', 'GATE_S2E_SHADOW_ENABLED': 'true',
           'GATE_S3_SHADOW_ENABLED': 'true', 'GATE_OMS_CONNECTION_ENABLED': 'false',
           'GATE_OMS_WRITES_ENABLED': 'false', 'SYSTEM_MODE': 'PAPER'}
    app = create_app(environment=env, storage_path=str(root), mount_check=lambda _: True,
                     market_fetch=market, stage1_shadow_runner=source,
                     stage1_sleep=parked, oms_connector=no_oms)
    initial_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    startup_http, enqueue, published, cached_http = [], [], [], []
    start = time.perf_counter()
    with TestClient(app) as client:
        deadline = time.monotonic()+90
        while True:
            t0 = time.perf_counter()
            response = client.get('/api/gate/status')
            startup_http.append((time.perf_counter()-t0)*1000)
            state = response.json()['forward_research']
            if state['status'] == 'BLOCKED':
                raise AssertionError(state)
            if state.get('journal_observations') == count and 'collector' in holder:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('Fresh process recovery exceeded 90 seconds')
            time.sleep(.01)
        recovery_ms = (time.perf_counter()-start)*1000
        for i in range(samples):
            t0 = time.perf_counter()
            send(holder['collector'], count+i)
            enqueue.append((time.perf_counter()-t0)*1000)
            wait_collector(holder['collector'], count+i+1, timeout=60)
            published.append((time.perf_counter()-t0)*1000)
            t1 = time.perf_counter()
            response = client.get('/api/gate/research/forward')
            cached_http.append((time.perf_counter()-t1)*1000)
            if response.status_code != 200 or response.json()['comparison']['source_observations'] != count+i+1:
                raise AssertionError('Cached report does not match committed count')
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        financial_error = oracle(response.json()['comparison'], closed=True)
        final = client.get('/api/gate/status').json()
        if final['execution_enabled'] or final['forward_research']['execution_authorized']:
            raise AssertionError('Isolated drill enabled execution')
    path = journal_path(root)
    storage = {name: candidate.stat().st_size if candidate.exists() else 0
               for name, candidate in (('database', path), ('wal', Path(str(path)+'-wal')),
                                       ('shm', Path(str(path)+'-shm')))}
    return {'history_observations': count, 'appended_observations': samples,
            'fresh_process_api_ready_ms': round(recovery_ms, 3),
            'peak_rss_mib': round(peak_rss, 3), 'initial_rss_mib': round(initial_rss, 3),
            'enqueue_ms': percentiles(enqueue), 'append_to_verified_cache_ms': percentiles(published),
            'cached_asgi_http_ms': percentiles(cached_http),
            'asgi_http_during_recovery_ms': percentiles(startup_http),
            'journal_storage_bytes': storage, 'journal_storage_total_bytes': sum(storage.values()),
            'financial_oracle_max_error': financial_error,
            'execution_authorized': False}


def scale_drill(root, count, samples, timeout=180):
    root = Path(root)
    root.mkdir()
    seed(root, count, bulk=True)
    try:
        result = subprocess.run([sys.executable, str(SCRIPT), '--child-scale', str(count),
                                 '--samples', str(samples), '--root', str(root)],
                                cwd=BACKEND, env=child_environment(), capture_output=True,
                                text=True, timeout=timeout, check=True)
    except subprocess.TimeoutExpired:
        return {'history_observations': count, 'status': 'TIMEOUT', 'timeout_seconds': timeout}
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'Isolated scale child failed: {exc.stderr[-2000:]}') from None
    data = json.loads(result.stdout)
    data['status'] = 'MEASURED'
    checks = {
        'enqueue_p99_ms': data['enqueue_ms']['p99'],
        'cached_asgi_http_p99_ms': data['cached_asgi_http_ms']['p99'],
        'fresh_process_api_ready_ms': data['fresh_process_api_ready_ms'],
        'append_to_verified_cache_p99_ms': data['append_to_verified_cache_ms']['p99'],
        'peak_rss_mib': data['peak_rss_mib'],
    }
    data['provisional_budget_checks'] = {key: value <= PROVISIONAL_BUDGETS[key]
                                         for key, value in checks.items()}
    return data


def source_metadata():
    modules = ['app/demo/gate_s3_collector.py', 'app/demo/gate_service.py',
               'app/demo/gate_stage1_shadow.py', 'app/strategy/gate_s3_durable_research.py',
               'app/strategy/gate_s3_forward_scorecard.py', 'app/strategy/gate_s3_equity_valuation.py',
               'app/strategy/gate_s3_paired_evidence.py']
    return {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'platform': platform.system()+' '+platform.machine(), 'logical_cpu_count': os.cpu_count(),
            'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=BACKEND, text=True).strip(),
            'runtime_modified_against_head': subprocess.check_output(
                ['git', 'diff', '--name-only', 'HEAD', '--', *modules], cwd=BACKEND, text=True).splitlines(),
            'runtime_sha256': {p: hashlib.sha256((BACKEND/p).read_bytes()).hexdigest() for p in modules},
            'harness_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child-fault', choices=PHASES)
    parser.add_argument('--child-scale', type=int)
    parser.add_argument('--child-resume', type=int)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--sizes', default='100,1000,5000,9900')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--skip-crash', action='store_true')
    args = parser.parse_args()
    if args.child_fault:
        fault_child(args.root, args.child_fault)
        return
    if args.child_scale is not None:
        print(json.dumps(scale_child(args.root, args.child_scale, args.samples)))
        return
    if args.child_resume is not None:
        print(json.dumps(resume_child(args.root, args.child_resume)))
        return
    sizes = [int(v) for v in args.sizes.split(',') if v]
    if not 1 <= args.samples <= 50 or any(n < 30 or n+args.samples >= 10000 for n in sizes):
        parser.error('Use 1..50 samples and 30 <= size < 10000 - samples')
    report = {'experiment': 'S3_ISOLATED_PROCESS_CRASH_AND_SCALE_V1',
              'synthetic_data_only': True, 'railway_modified': False,
              'hardware_or_power_loss_tested': False, 'os_page_cache_cold_proven': False,
              'fixture_setup_not_measured': 'bulk insert, closed connections, then full verified recovery',
              'real_execution_proven': False, 'profitability_proven': False,
              'method': 'nearest-rank descriptive percentiles; in-process ASGI; separate fresh OS process per scale',
              'provisional_local_budgets_not_hosted_sla': PROVISIONAL_BUDGETS,
              'metadata': source_metadata(), 'crash_cases': [], 'scale': []}
    with tempfile.TemporaryDirectory(prefix='capital-cipher-s3-drill-') as temporary:
        base = Path(temporary)
        if not args.skip_crash:
            for phase in PHASES:
                data = crash_drill(base/phase, phase)
                report['crash_cases'].append(data)
                print(json.dumps({'completed_crash_case': data}), flush=True)
        for count in sizes:
            data = scale_drill(base/f'scale-{count}', count, args.samples)
            report['scale'].append(data)
            print(json.dumps({'completed_scale': data}), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2)+'\n')
    else:
        print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()

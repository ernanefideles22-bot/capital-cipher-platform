import copy
from datetime import datetime,timezone,timedelta
import gzip
import hashlib
import json

import pytest

from app.strategy import gate_scenario_profitability_audit as a


def packed(rows):
    return gzip.compress(('\n'.join(','.join(map(str,r)) for r in rows)+'\n').encode())


def test_price_csv_field_order_volume_and_open_close_timestamp_are_explicit():
    t=a.SOURCE_START
    rows=a.parse_prices(packed([[t,12,101,102,99,100]]),'202311')
    assert rows == [{'t':t,'v':12.,'c':101.,'h':102.,'l':99.,'o':100.}]
    report=a.context_readiness([t+i*60 for i in range(15)],source_start=t,
        evaluation_start=t,end=t+900,lookback=1)
    assert report['complete_aggregate_bars']['15m'] == 1
    assert report['boundaries_with_complete_15m_1h_4h_context'] == 0


@pytest.mark.parametrize('row',[
    [a.SOURCE_START,1,float('nan'),102,99,100],
    [a.SOURCE_START,1,103,102,99,100],
    [a.SOURCE_START,-1,101,102,99,100],
    [a.SOURCE_START+1,1,101,102,99,100],
    [a.SOURCE_START-60,1,101,102,99,100],
    [a.SOURCE_START,1,101,102,99],
])
def test_bad_schema_ohlcv_clock_and_month_are_not_imputed(row):
    with pytest.raises(ValueError):
        a.parse_prices(packed([row]),'202311')


def test_duplicates_and_reordering_are_not_silently_deduplicated():
    r=[a.SOURCE_START,1,100,101,99,100]
    with pytest.raises(ValueError,match='Duplicate'):
        a.parse_prices(packed([r,r]),'202311')
    with pytest.raises(ValueError,match='Duplicate'):
        a.parse_prices(packed([[r[0]+60,*r[1:]],r]),'202311')
    with pytest.raises(ValueError,match='Duplicate'):
        a.gap_report([r[0],r[0]],start=r[0],end=r[0]+60)


def test_gap_counts_use_exclusive_end_and_preserve_consecutive_ranges():
    t=a.SOURCE_START
    v=a.gap_report([t,t+180,t+300],start=t,end=t+300)
    assert (v['expected_minutes'],v['observed_minutes'],v['missing_minutes']) == (5,2,3)
    assert [r['missing_minutes'] for r in v['gap_ranges']] == [2,1]
    assert v['imputed_or_forward_filled_minutes'] == 0


def test_missing_minute_breaks_higher_timeframe_continuity_until_warmup_is_restored():
    first=a.SOURCE_START
    full=list(range(first,first+12*3600,60))
    ready=a.context_readiness(full,source_start=first,evaluation_start=first+8*3600,
                             end=first+12*3600,lookback=2)
    assert ready['boundaries_with_complete_15m_1h_4h_context'] == 16
    full.remove(first+60)
    gap=a.context_readiness(full,source_start=first,evaluation_start=first+8*3600,
                           end=first+12*3600,lookback=2)
    assert gap['boundaries_with_complete_15m_1h_4h_context'] == 0
    assert gap['longest_consecutive_complete_bars']['4h'] == 2


def test_executed_funding_sign_is_preserved_and_future_month_refused():
    t=a.SOURCE_START
    assert a.parse_funding(packed([[t,-.0001]]),'202311')[0]['r'] == -.0001
    with pytest.raises(ValueError):
        a.parse_funding(packed([[t,.0001]]),'202312')


def fixture_source(root, *, missing=False):
    raw_dir=root/'raw';raw_dir.mkdir()
    declared=datetime(2026,11,4,tzinfo=timezone.utc)
    intent={'declared_at_utc':declared.isoformat(),'fixed_market':'BTC_USDT',
        'price_source_months':['202311','202312'],'fixed_evaluation_start':'2023-11-15T00:00:00Z',
        'fixed_evaluation_end':'2023-12-15T00:00:00Z','warmup_start':'2023-11-01T00:00:00Z',
        'source_get_budget':5,'parent_observer_commit':'ec1917a99413119ed009ca307d80ec5ad60bc94e',
        'execution_authorized':False}
    (root/'source-intent.json').write_text(json.dumps(intent))
    for name,url in a.expected_sources().items():
        if name.startswith('candlesticks_'):
            month=name.split('-')[1][:6]
            rows=[[t,1,100,101,99,100] for t in range(a.SOURCE_START,a.END,60)
                  if datetime.fromtimestamp(t,timezone.utc).strftime('%Y%m')==month
                  and not (missing and t==a.EVALUATION_START+60)]
            raw=packed(rows)
        elif name.startswith('funding_'):
            month=name.split('-')[1][:6]
            raw=packed([[t,.0001] for t in range(a.SOURCE_START,a.END+1,28800)
                if datetime.fromtimestamp(t,timezone.utc).strftime('%Y%m')==month])
        else:
            raw=json.dumps([{'t':t,'o':100,'h':101,'l':99,'c':100}
                for t in range(a.SOURCE_START,a.END+14401,14400)]).encode()
        (raw_dir/name).write_bytes(raw)
        receipt={'url':url,'status':200,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'requested_at_utc':(declared+timedelta(seconds=1)).isoformat(),
            'received_at_utc':(declared+timedelta(seconds=2)).isoformat()}
        (raw_dir/(name+'.receipt.json')).write_text(json.dumps(receipt))


def test_complete_market_data_still_cannot_make_an_observer_a_profitable_execution_strategy(tmp_path):
    fixture_source(tmp_path)
    report=a.audit(tmp_path)
    assert report['full_window']['missing_minutes'] == 0
    assert report['technical_context']['boundaries_with_complete_15m_1h_4h_context'] == 2880
    assert report['status'] == 'INCONCLUSIVE_NOT_FINANCIALLY_EVALUATED'
    assert all(v=={'status':'NOT_COMPUTED','value':None} for v in report['profitability_metrics'].values())
    assert not report['strategy_declared_unprofitable'] and not report['profitability_proven']
    assert report['new_financial_replays'] == 0


def test_raw_receipt_corruption_is_refused_before_any_result(tmp_path):
    fixture_source(tmp_path)
    file=tmp_path/'raw/candlesticks_1m-202311.csv.gz'
    file.write_bytes(file.read_bytes()+b'changed')
    with pytest.raises(ValueError,match='receipt/hash'):
        a.audit(tmp_path)


def test_gap_is_reported_without_claiming_losing_trades_or_a_zero_return(tmp_path):
    fixture_source(tmp_path,missing=True)
    report=a.audit(tmp_path)
    assert report['evaluation_window']['missing_minutes'] == 1
    assert 'HISTORICAL_MINUTE_GRID_INCOMPLETE' in report['blockers']
    assert report['profitability_metrics']['net_pnl']['value'] is None
    assert report['execution_authorized'] is False


def test_decompression_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(a,'MAX_DECOMPRESSED_BYTES',10)
    with pytest.raises(ValueError,match='Decompressed'):
        a.decompress(gzip.compress(b'x'*11))


def test_changed_parent_observer_requires_a_separate_review(tmp_path,monkeypatch):
    monkeypatch.setattr(a,'OBSERVER_SHA256','changed')
    with pytest.raises(ValueError,match='frozen PR99'):
        a.audit(tmp_path)

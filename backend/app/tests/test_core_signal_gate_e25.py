"""E2.5: fixed SPOT long-only filter causality, protocol and real-bar tests."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.core_signal_gate_e25 import (
    EXPERIMENT, PAIRS, experiment_digest, make_filtered_long_only,
)
from app.strategy.matched_signal_replay_e21 import SignalEvent
from scripts.run_e25_frozen_spot_signal_gate import load_frozen_csv, run

FIXTURES=Path(__file__).resolve().parents[2]/"fixtures"/"market_samples"


def candles(n=90, *, drift=1.003, vol=100):
    start=datetime(2026,1,1,tzinfo=timezone.utc)
    bars=[]
    for i in range(n):
        price=100*drift**i
        close=start+timedelta(minutes=15*(i+1))
        bars.append(Candle(
            exchange=Exchange.BINANCE,symbol="BTCUSDT",timeframe="15m",
            open=price*0.999, high=price*1.002,low=price*0.998,
            close=price,volume=vol,
            closed_at=close,received_at=close+timedelta(seconds=1),
        ))
    return bars


def test_protocol_preregistered_not_live_or_backtest_optimized():
    assert len(experiment_digest()) == 64
    assert EXPERIMENT["venue"]=="BINANCE_SPOT_LONG_ONLY"
    assert EXPERIMENT["automatic_promotion"] is False
    assert EXPERIMENT["research_only"]
    assert EXPERIMENT["fees_per_side_bps"]==[12.0,25.0,40.0]
    assert EXPERIMENT["evaluation_start_index"]==300
    assert len(PAIRS)==3


def test_long_only_gate_cannot_create_new_signals():
    bars=candles()
    baseline=[
        SignalEvent(i,bars[i].received_at,"BUY",.5)
        for i in range(60,75)
    ]
    candidate=make_filtered_long_only(baseline,bars,evaluation_start=60)
    assert len(candidate)>0
    assert all(x in baseline for x in candidate)
    assert all(x.side=="BUY" for x in candidate)


def test_fail_closed_on_sell_duplicate_or_lookahead():
    bars=candles()
    buy=SignalEvent(60,bars[60].received_at,"BUY",.5)
    sell=SignalEvent(61,bars[61].received_at,"SELL",.5)
    with pytest.raises(ValueError,match="long-only"):
        make_filtered_long_only([sell],bars,evaluation_start=60)
    with pytest.raises(ValueError,match="Duplicate"):
        make_filtered_long_only([buy,buy],bars,evaluation_start=60)
    early=SignalEvent(60,bars[60].closed_at,"BUY",.5)
    with pytest.raises(ValueError,match="predates"):
        make_filtered_long_only([early],bars,evaluation_start=60)


def test_real_spot_input_disjoint_and_immutable_cutoff(tmp_path):
    for symbol,tf in PAIRS:
        file=FIXTURES/f"e25_{symbol}_{tf}_spot_cutoff_20260601.csv"
        bars,meta=load_frozen_csv(file,symbol,tf)
        assert len(bars)==1000
        assert bars[-1].closed_at.isoformat()=="2026-06-01T00:00:00+00:00"
        assert bars[0].closed_at < bars[-1].closed_at
        assert not meta["real_spreads_and_fills_not_observed"] is False
        assert len(meta["file_sha256"])==64
    # Reject altered/horizon-breaking files, never quietly adapt the protocol.
    file=FIXTURES/"e25_BTCUSDT_15m_spot_cutoff_20260601.csv"
    lines=file.read_text().splitlines()
    lines.pop(10)
    path=tmp_path/"missing.csv"
    path.write_text("\n".join(lines)+"\n")
    with pytest.raises(ValueError,match="row count"):
        load_frozen_csv(path,"BTCUSDT","15m")
    lines=file.read_text().splitlines()
    lines[-1]=lines[-2]
    path.write_text("\n".join(lines)+"\n")
    with pytest.raises(ValueError,match="Non-contiguous"):
        load_frozen_csv(path,"BTCUSDT","15m")


@pytest.mark.asyncio
async def test_historical_core_buy_against_filtered_model_never_promotes():
    result=await run(FIXTURES)
    assert result["mode"]=="E25_DISJOINT_HISTORICAL_EXPLORATION_NOT_PROSPECTIVE"
    assert result["only_three_core_agents_not_all_300"]
    assert result["not_live_not_testnet_not_deployed"]
    assert not result["strategy_promotion_authorized"]
    assert not result["profitability_proven"]
    assert len(result["results"])==3
    for series in result["results"]:
        assert series["filtered_buy_signals"]<=series["baseline_buy_signals"]
        assert len(series["comparisons"])==3
        for x in series["comparisons"]:
            baseline=x["baseline_core_buy"]
            candidate=x["frozen_trend_gated_core_buy"]
            assert baseline["common_model_id"]==candidate["common_model_id"]
            assert baseline["cost_bps_per_side"]==candidate["cost_bps_per_side"]
            assert baseline["bar_count"]==candidate["bar_count"]==1000
            assert not baseline["execution_authorized"]
            assert not candidate["execution_authorized"]
            assert not x["automated_promotion_authorized"]

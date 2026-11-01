"""Stage 1 Gate professional chart reader: causal, deterministic, orderless."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from math import sin, pi

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.professional_gate_chart_stage1 import (
    MIN_BARS, analyze_gate_chart, analyze_gate_multi_timeframe,
)

END=datetime(2026,11,1,12,tzinfo=timezone.utc)
STEP={"15m":900,"1h":3600,"4h":14400}


def candles(tf="15m", count=120, *, exchange=Exchange.GATE,
            symbol="BTCUSDT", wobble=False, descending=False,
            received_all_at=None):
    step=timedelta(seconds=STEP[tf])
    start=END-count*step
    result=[]
    for i in range(count):
        price=100+(-0.25*i if descending else 0.25*i)
        if wobble:
            price += 3*sin(i*pi/4)
        when=start+(i+1)*step
        result.append(Candle(
            exchange=exchange,symbol=symbol,timeframe=tf,
            open=price, high=price+1.5,low=price-1.5,
            close=price+0.2,volume=100,
            closed_at=when,received_at=received_all_at or when+timedelta(seconds=1),
        ))
    return result


def analyze(bars, at=END+timedelta(seconds=3)):
    return analyze_gate_chart(bars,as_of=at)


def test_gate_chart_is_a_real_deterministic_research_report_not_an_order():
    bars=candles(wobble=True)
    a=analyze(bars)
    assert a==analyze(bars)
    assert a.exchange=="GATE"
    assert a.symbol=="BTCUSDT"
    assert a.candles_analyzed==120
    assert a.ema20>a.ema50
    assert 0<=a.rsi14<=100
    assert a.atr14>0
    assert a.pivot_highs
    assert a.pivot_lows
    assert all(x.confirmed_at>x.occurred_at for x in a.pivot_highs+a.pivot_lows)
    assert all(x.confirmed_at<=a.as_of for x in a.pivot_highs+a.pivot_lows)
    assert not a.data_source_authenticated
    assert not a.predictive_edge_proven
    assert not a.trading_authorized
    with pytest.raises(FrozenInstanceError):
        a.regime="UPTREND"


def test_market_structure_and_fibonacci_use_confirmed_extremes_only():
    bars=candles(wobble=True)
    report=analyze(bars)
    assert report.market_structure in {
        "HIGHER_HIGHS_HIGHER_LOWS","MIXED_STRUCTURE",
    }
    assert report.fibonacci_leg in {"UPSWING","DOWNSWING"}
    assert len(report.fibonacci_retracements)==3
    fib=[x[1] for x in report.fibonacci_retracements]
    assert fib==sorted(fib) or fib==sorted(fib,reverse=True)
    assert report.support is None or report.support <= bars[-1].close
    assert report.resistance is None or report.resistance >= bars[-1].close


def test_pivot_never_appears_before_two_confirmation_bars():
    bars=candles(count=83)
    bars[80]=bars[80].model_copy(update={"high":1000})
    prefix=analyze(bars[:82],at=bars[81].received_at+timedelta(seconds=1))
    full=analyze(bars,at=bars[82].received_at+timedelta(seconds=1))
    assert 80 not in [p.index for p in prefix.pivot_highs]
    assert 80 in [p.index for p in full.pivot_highs]


def test_bullish_engulfing_is_morphology_only_not_buy_approval():
    bars=candles()
    bars[-2]=bars[-2].model_copy(update={
        "open":105,"close":103,"high":106,"low":102,
    })
    bars[-1]=bars[-1].model_copy(update={
        "open":102.5,"close":105.5,"high":106.5,"low":102,
    })
    r=analyze(bars)
    assert "BULLISH_ENGULFING" in r.candle_patterns
    assert not r.trading_authorized


def test_hammer_shape_and_doji_must_be_assessed_in_context():
    bars=candles()
    bars[-1]=bars[-1].model_copy(update={
        "open":105,"close":105.4,"high":105.6,"low":103,
    })
    assert "HAMMER_SHAPE" in analyze(bars).candle_patterns
    bars[-1]=bars[-1].model_copy(update={
        "open":105,"close":105.01,"high":106,"low":104,
    })
    assert "DOJI" in analyze(bars).candle_patterns


@pytest.mark.parametrize("tf",["15m","1h","4h"])
def test_all_three_gate_timeframes_read_without_orders(tf):
    r=analyze(candles(tf=tf))
    assert r.timeframe==tf
    assert r.candles_analyzed==120
    assert not r.trading_authorized


def test_three_timeframe_confluence_is_causal_and_cross_symbol_forbidden():
    groups={tf:candles(tf) for tf in ("15m","1h","4h")}
    r=analyze_gate_multi_timeframe(groups,as_of=END+timedelta(seconds=3))
    assert [v.timeframe for v in r.readings]==["4h","1h","15m"]
    assert r.alignment=="BULLISH_ALIGNMENT"
    assert not r.trading_authorized
    groups["1h"]=candles("1h",symbol="ETHUSDT")
    with pytest.raises(ValueError,match="Mixed venues"):
        analyze_gate_multi_timeframe(groups,as_of=END+timedelta(seconds=3))


def test_conflicting_timeframes_are_not_falsely_marked_as_aligned():
    groups={tf:candles(tf) for tf in ("15m","1h","4h")}
    groups["4h"]=candles("4h",descending=True)
    r=analyze_gate_multi_timeframe(groups,as_of=END+timedelta(seconds=3))
    assert r.alignment=="MIXED_OR_UNCONFIRMED"


@pytest.mark.parametrize("mutator,pattern",[
    (lambda b:b[:-1],"Stale"), # last is older only for a separately older cutoff test below
])
def test_window_without_required_research_bars_is_rejected(mutator,pattern):
    with pytest.raises(ValueError,match="80"):
        analyze(candles(count=79))


def test_source_is_gate_futures_not_legacy_binance():
    with pytest.raises(ValueError,match="GATE"):
        analyze(candles(exchange=Exchange.BINANCE))


def test_wrong_tf_gaps_duplicates_future_receipt_and_stale_fail_closed():
    bars=candles()
    with pytest.raises(ValueError,match="irregular"):
        analyze(bars[:50]+bars[51:])
    with pytest.raises(ValueError,match="irregular"):
        analyze(bars[:50]+[bars[49]]+bars[50:])
    with pytest.raises(ValueError,match="Future or not-yet-received"):
        analyze_gate_chart(bars,as_of=bars[-1].closed_at)
    with pytest.raises(ValueError,match="Stale"):
        analyze(bars,at=END+timedelta(hours=1))
    with pytest.raises(ValueError,match="clock"):
        analyze_gate_chart(bars,as_of=datetime(2026,11,1,12))


def test_bad_receipt_and_invalid_price_fail_closed():
    bars=candles()
    bars[-1]=bars[-1].model_copy(update={
        "received_at":bars[-1].closed_at-timedelta(seconds=1),
    })
    with pytest.raises(ValueError,match="Receipt before"):
        analyze(bars)
    bars=candles()
    bars[-1]=bars[-1].model_copy(update={"volume":float("inf")})
    with pytest.raises(ValueError,match="Non-finite"):
        analyze(bars)
    bars=candles()
    bars[-1]=bars[-1].model_copy(update={"high":1})
    with pytest.raises(ValueError,match="Invalid OHLCV"):
        analyze(bars)


def test_historical_batch_receipt_is_allowed_but_does_not_authenticate_source():
    bars=candles(received_all_at=END+timedelta(seconds=1))
    r=analyze(bars)
    assert r.candles_analyzed==120
    assert not r.data_source_authenticated


def test_multitimeframe_rejects_incomplete_set_and_higher_tf_future_leak():
    groups={tf:candles(tf) for tf in ("15m","1h","4h")}
    with pytest.raises(ValueError,match="15m,1h,4h"):
        analyze_gate_multi_timeframe({"15m":groups["15m"]},as_of=END)
    with pytest.raises(ValueError,match="Future or not-yet-received"):
        analyze_gate_multi_timeframe(
            groups,as_of=END-timedelta(minutes=30),
        )

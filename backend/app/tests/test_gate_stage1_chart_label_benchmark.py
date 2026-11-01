"""Independently hand-labelled deterministic pattern and causal boundary fixtures.

Labels are *shape* classification, not calibrated predictive trade signals.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.professional_gate_chart_stage1 import (
    ConfirmedPivot, _patterns, _chart_patterns, analyze_gate_chart,
)

BASE=datetime(2026,11,1,4,tzinfo=timezone.utc)


def bar(index, op,hi,lo,cl):
    closed=BASE+timedelta(minutes=15*index)
    return Candle(exchange=Exchange.GATE,symbol="BTCUSDT",timeframe="15m",
        open=op,high=hi,low=lo,close=cl,volume=10,
        closed_at=closed,received_at=closed+timedelta(seconds=2))


@pytest.mark.parametrize("previous,last,required,excluded", [
    ((100,102,98,101),(101,102,99,101.02), "DOJI","LONG_BODY"),
    ((100,101,97,98),(97.5,102,97,101.5),"BULLISH_ENGULFING","BEARISH_ENGULFING"),
    ((100,102,99,101),(101.5,102,98,99.5),"BEARISH_ENGULFING","BULLISH_ENGULFING"),
    ((100,101,99,100.5),(100,100.5,94,100.4),"HAMMER_SHAPE","SHOOTING_STAR_SHAPE"),
    ((100,101,99,100.5),(100,106,99.7,100.4),"SHOOTING_STAR_SHAPE","HAMMER_SHAPE"),
    ((100,106,94,102),(100,105,95,101),"INSIDE_BAR","OUTSIDE_BAR"),
    ((100,105,95,102),(100,106,94,101),"OUTSIDE_BAR","INSIDE_BAR"),
    ((100,102,98,101),(99,110,99,109.8),"LONG_BODY","DOJI"),
])
def test_hand_labelled_candle_morphology(previous,last,required,excluded):
    labels=_patterns([bar(0,*previous),bar(1,*last)])
    assert required in labels
    assert excluded not in labels


def test_double_top_requires_two_confirmed_highs_and_intermediate_neckline():
    t=BASE
    highs=(ConfirmedPivot("HIGH",10,110,t,t+timedelta(minutes=30)),
           ConfirmedPivot("HIGH",22,109.8,t,t+timedelta(minutes=30)))
    lows=(ConfirmedPivot("LOW",16,100,t,t+timedelta(minutes=30)),)
    assert _chart_patterns(highs,lows,1.,104)==("POTENTIAL_DOUBLE_TOP",)
    assert _chart_patterns(highs,lows,1.,99)==("DOUBLE_TOP_BREAKDOWN",)
    assert _chart_patterns(highs,(),1.,99)==()


def test_double_bottom_breakout_requires_neckline_confirmation():
    t=BASE
    lows=(ConfirmedPivot("LOW",10,90,t,t+timedelta(minutes=30)),
          ConfirmedPivot("LOW",22,90.25,t,t+timedelta(minutes=30)))
    highs=(ConfirmedPivot("HIGH",16,100,t,t+timedelta(minutes=30)),)
    assert _chart_patterns(highs,lows,1.,95)==("POTENTIAL_DOUBLE_BOTTOM",)
    assert _chart_patterns(highs,lows,1.,101)==("DOUBLE_BOTTOM_BREAKOUT",)


def test_prefix_invariance_and_two_right_bar_pivot_confirmation():
    seq=[]
    for i in range(90):
        x=100+.15*i
        seq.append(bar(i,x,x+1,x-1,x+.1))
    seq[83]=seq[83].model_copy(update={"high":300})
    for end in (84,85,86):
        sliced=seq[:end]
        r=analyze_gate_chart(sliced,as_of=sliced[-1].received_at)
        confirmed=[p.index for p in r.pivot_highs]
        assert (83 in confirmed)==(end>=86)
        assert all(p.confirmed_at <= r.as_of for p in r.pivot_highs)
        assert r.trading_authorized is False
        assert r.predictive_edge_proven is False

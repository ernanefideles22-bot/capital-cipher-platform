"""S2C causal cross-stage confluence, independent clock, no-OMS regression."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import ResearchPlan
from app.strategy.gate_s2_replay import Friction,replay_segment
from app.strategy import gate_s2_replay
from app.strategy.gate_s2c_confluence import (
    HistoricalConfluenceEvaluator,evaluate_confluence,
)
from app.strategy.professional_gate_chart_stage1 import (
    analyze_gate_multi_timeframe,
)

END=datetime(2026,11,1,12,tzinfo=timezone.utc)
STEPS={"15m":900,"1h":3600,"4h":14400}


def make_bars(tf,n=160,*,descending=False):
    step=timedelta(seconds=STEPS[tf])
    out=[]
    for i in range(n):
        x=100 +(-.5 if descending else .5)*i
        closed=END-(n-1-i)*step
        out.append(Candle(
            exchange=Exchange.GATE,symbol="BTCUSDT",timeframe=tf,
            open=x,high=x+1.2,low=x-1.1,close=x+.3,
            volume=100,closed_at=closed,
            received_at=closed+timedelta(microseconds=1)))
    return out


def context(*,down4=False,down1=False):
    groups={
        "15m":make_bars("15m"),
        "1h":make_bars("1h",descending=down1),
        "4h":make_bars("4h",descending=down4),
    }
    return analyze_gate_multi_timeframe(groups,as_of=END+timedelta(seconds=1))


def plan(side="LONG",strategy_id="EMA_TREND_PULLBACK_V1"):
    return ResearchPlan(
        strategy_id=strategy_id,strategy_version="1.0.0",
        symbol="BTCUSDT",timeframe="15m",cutoff=END+timedelta(seconds=1),
        action=side,stop_distance=1.5,target_distance=3.,
        reasoning="UNIT_TEST_HYPOTHESIS",execution_authorized=False,
    )


def test_uptrend_multi_timeframe_allows_long_only_as_research():
    c=context()
    assert c.alignment=="BULLISH_ALIGNMENT"
    good=evaluate_confluence(plan("LONG"),c)
    assert good.status=="ALLOW_RESEARCH"
    assert good.filtered_action=="LONG"
    assert good.authority_to_trade is False
    assert good.empirical_improvement_proven is False
    short=evaluate_confluence(plan("SHORT"),c)
    assert short.filtered_action=="WAIT"
    assert "HIGHER_TIMEFRAMES_NOT_ALIGNED" in short.reasons


def test_higher_timeframe_conflict_and_range_trend_are_rejected():
    conflicting=context(down4=True)
    assert conflicting.alignment=="MIXED_OR_UNCONFIRMED"
    outcome=evaluate_confluence(plan("LONG"),conflicting)
    assert outcome.status=="REJECT_RESEARCH"
    assert "HIGHER_TIMEFRAMES_NOT_ALIGNED" in outcome.reasons
    ranged=evaluate_confluence(plan("LONG","RANGE_BOLLINGER_REVERSION_V1"),context())
    assert "HIGHER_TIMEFRAME_TREND_AGAINST_MEAN_REVERSION" in ranged.reasons


def test_rejects_bad_clock_mixed_symbol_unsafe_authority_and_missing_tf():
    c=context()
    p=plan()
    with pytest.raises(ValueError,match="clocks disagree"):
        evaluate_confluence(replace(p,cutoff=p.cutoff+timedelta(seconds=1)),c)
    with pytest.raises(ValueError,match="symbol or venue"):
        evaluate_confluence(replace(p,symbol="SOLUSDT"),c)
    with pytest.raises(ValueError,match="Executable"):
        evaluate_confluence(replace(p,execution_authorized=True),c)
    with pytest.raises(ValueError,match="unique"):
        evaluate_confluence(p,replace(c,readings=c.readings[:2]))
    with pytest.raises(ValueError,match="Stale"):
        stale=replace(c.readings[0],last_close_at=END-timedelta(days=3))
        evaluate_confluence(p,replace(c,readings=(stale,*c.readings[1:])))


def test_confluence_never_turns_no_entry_into_trade():
    verdict=evaluate_confluence(
        replace(plan(),action="WAIT",stop_distance=None,target_distance=None),
        context())
    assert verdict.filtered_action=="WAIT"
    assert verdict.reasons==("NO_S2_ENTRY_PROPOSAL",)


def test_historical_uses_only_closed_prefix_higher_candles_and_never_mutates_receipts():
    base_higher={"1h":make_bars("1h"),"4h":make_bars("4h")}
    # Simulate the entire Gate historical batch first fetched hours later.
    batch_at=END+timedelta(days=1)
    for tf in base_higher:
        base_higher[tf]=[
            bar.model_copy(update={"received_at":batch_at})
            for bar in base_higher[tf]]
    primary=make_bars("15m")
    cutoff=primary[-15].closed_at+timedelta(microseconds=1)
    left=HistoricalConfluenceEvaluator(base_higher)
    first=left._time_locked_chart(primary[:-14],cutoff)
    augmented={}
    for tf,rows in base_higher.items():
        later=rows[-1].model_copy(update={
            "closed_at":rows[-1].closed_at+timedelta(seconds=STEPS[tf]),
            "received_at":batch_at+timedelta(days=1),
            "high":rows[-1].high+1000,
        })
        augmented[tf]=rows+[later]
    right=HistoricalConfluenceEvaluator(augmented)
    second=right._time_locked_chart(primary[:-14],cutoff)
    assert first==second
    assert all(r.last_close_at<=cutoff for r in first.readings)
    assert all(p.confirmed_at<=cutoff
        for r in first.readings for p in r.pivot_highs+r.pivot_lows)
    assert base_higher["4h"][0].received_at==batch_at
    assert first.trading_authorized is False


def test_closed_15m_signal_can_be_filtered_by_real_stage1_in_replay(monkeypatch):
    primary=make_bars("15m")
    evaluator=HistoricalConfluenceEvaluator({
        "1h":make_bars("1h"),
        "4h":make_bars("4h",descending=True),
    })
    def synthetic_plan(strategy_id, candles,*,as_of,historical_replay):
        assert historical_replay
        assert candles[-1].closed_at<as_of
        return ResearchPlan(
            strategy_id=strategy_id,strategy_version="1.0.0",
            symbol=candles[0].symbol,timeframe=candles[0].timeframe,
            cutoff=as_of,action="LONG",
            stop_distance=1.,target_distance=2.,
            reasoning="SIMULATED_SIGNAL_ONLY",execution_authorized=False,
        )
    import app.strategy.gate_s2c_confluence as s2c
    monkeypatch.setattr(s2c,"evaluate_strategy",synthetic_plan)
    monkeypatch.setattr(gate_s2_replay,"evaluate_strategy",synthetic_plan)
    baseline=replay_segment(
        "EMA_TREND_PULLBACK_V1",primary,start_index=130,
        end_index=160,segment="TEST",friction=Friction(0,0,0))
    gated=replay_segment(
        "EMA_TREND_PULLBACK_V1",primary,start_index=130,
        end_index=160,segment="TEST",friction=Friction(0,0,0),
        plan_evaluator=evaluator.evaluate)
    assert len(baseline.trades)>0
    assert len(gated.trades)==0
    assert evaluator.counts["HIGHER_TIMEFRAMES_NOT_ALIGNED"]>0
    assert not gated.execution_authorized


def test_replay_rejects_plan_with_order_authority_or_wrong_cutoff():
    primary=make_bars("15m")
    def illegal(strategy, prefix, cutoff):
        return replace(
            ResearchPlan(strategy,"1.0.0",prefix[0].symbol,
                         prefix[0].timeframe,cutoff,"LONG",
                         1.,2.,"NOT_ALLOWED",False,False),
            execution_authorized=True)
    with pytest.raises(ValueError,match="Untrusted"):
        replay_segment("EMA_TREND_PULLBACK_V1",primary,
                       start_index=130,end_index=145,segment="TEST",
                       plan_evaluator=illegal)
    def wrong_time(strategy,prefix,cutoff):
        return ResearchPlan(strategy,"1.0.0",prefix[0].symbol,
                            prefix[0].timeframe,cutoff+timedelta(minutes=1),
                            "LONG",1.,2.,"BAD_TIME",False,False)
    with pytest.raises(ValueError,match="Untrusted"):
        replay_segment("EMA_TREND_PULLBACK_V1",primary,
                       start_index=130,end_index=145,segment="TEST",
                       plan_evaluator=wrong_time)

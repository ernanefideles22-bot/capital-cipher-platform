from dataclasses import replace
from datetime import datetime, timedelta, timezone
import copy

import pytest

from app.schemas.market import Candle
from app.strategy import gate_scenario_observer as v1
from app.strategy import gate_scenario_staged_levels as v2
from app.strategy.professional_gate_chart_stage1 import ConfirmedPivot, MultiTimeframeReading, ProfessionalChartReading

START = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def pivot(kind, price, minute, confirmed, side="SHORT"):
    if side == "LONG":
        kind, price = ("LOW" if kind == "HIGH" else "HIGH"), 200-price
    return ConfirmedPivot(kind, minute, price, START+timedelta(minutes=minute),
                          START+timedelta(minutes=confirmed, seconds=1))


def chart(index, *, side="SHORT", extra=None, without_leg=False, new_confirmed=30):
    now = START+timedelta(minutes=index, seconds=2)
    high = (pivot("HIGH",105,-120,-90,side),)
    low = (pivot("LOW",70,-180,-150,side), pivot("LOW",78,-60,-30,side))
    if without_leg:
        low = low[:1]
    if index >= new_confirmed:
        low += (pivot("LOW",99,0,new_confirmed,side),)
    if extra:
        low += (extra,)
    if side == "LONG":
        high,low = low,high
    readings = tuple(ProfessionalChartReading(
        exchange="GATE",symbol="BTCUSDT",timeframe=tf,as_of=now,
        last_close_at=START+timedelta(minutes=index//15*15) if tf=="15m" else START,
        candles_analyzed=80,regime="DOWNTREND" if side=="SHORT" else "UPTREND",
        ema20=101,ema50=102,atr14=5,relative_volume=1,rsi14=50,macd_histogram=0,
        candle_patterns=(),chart_patterns=(),
        market_structure="LOWER_HIGHS_LOWER_LOWS" if side=="SHORT" else "HIGHER_HIGHS_HIGHER_LOWS",
        pivot_highs=high,pivot_lows=low,support=78,resistance=105,
        support_trend_slope_atr_per_bar=None,resistance_trend_slope_atr_per_bar=None,
        fibonacci_leg="DOWNSWING",fibonacci_retracements=(),breakout="NONE"
    ) for tf in ("4h","1h","15m"))
    return MultiTimeframeReading("GATE","BTCUSDT",now,"MIXED_OR_UNCONFIRMED",readings)


def candle(index, *, tf="1m", high=None, low=None, close=None, side="SHORT"):
    if tf == "15m":
        values = {0:(101,99,100),15:(106,103,105),30:(105.5,102,103),
                  45:(103.5,97,98),60:(100,97,98)}[index]
    else:
        p=100 if index<15 else 105 if index<30 else 103 if index<45 else 97.9
        values=(p+.1,p-.1,p)
    h,l,c=values
    h,l,c=h if high is None else high,l if low is None else low,c if close is None else close
    if side == "LONG":
        h,l,c=200-l,200-h,200-c
    at=START+timedelta(minutes=index)
    return Candle(exchange="GATE",symbol="BTCUSDT",timeframe=tf,open=c,
        high=h,low=l,close=c,volume=100,closed_at=at,received_at=at+timedelta(seconds=1))


def seeded(side="SHORT", **kw):
    plan,_=v2.create_plan(chart(0,side=side,**kw),100)
    return plan,v2.initial_state(plan)


def observe(plan,state,index, *, reading=None, minute=None, bar=None):
    side=plan["side"]
    return v2.advance(plan,state,reading or chart(index,side=side),
        bar or candle(index//15*15,tf="15m",side=side),minute or candle(index,side=side),
        as_of=START+timedelta(minutes=index,seconds=2))


def through(stop, *, side="SHORT", reader=None):
    plan,state=seeded(side)
    for index in range(1,stop+1):
        state=observe(plan,state,index,reading=reader(index) if reader else None)
    return plan,state


@pytest.mark.parametrize("side",["SHORT","LONG"])
def test_continuous_minutes_form_local_trigger_then_separate_break_and_recheck(side):
    plan,state=seeded(side)
    frozen=copy.deepcopy(plan)
    assert "trigger" not in plan and "target" not in plan
    seen=[state["stage"]]
    for index in range(1,47):
        state=observe(plan,state,index)
        if state["stage"] != seen[-1]: seen.append(state["stage"])
    assert seen == ["WAIT_TOUCH","WAIT_REJECTION","WAIT_BREAK","WAIT_RECHECK","CANDIDATE_ONLY"]
    assert plan == frozen
    assert state["armed"]["trigger"] == (99 if side=="SHORT" else 101)
    assert state["armed"]["target"] == (78 if side=="SHORT" else 122)
    assert state["stop"] == (106.5 if side=="SHORT" else 93.5)
    assert state["candidate"]["net_reward_risk"] >= 2
    assert not state["candidate"]["execution_authorized"]
    assert not state["candidate"]["profitability_proven"]


def test_v1_pre_touch_trigger_and_v2_post_rejection_trigger_are_distinct_hypotheses():
    old,_=v1.create_plan(chart(0),100)
    new,state=through(30)
    assert old["trigger"] == 78 and state["armed"]["trigger"] == 99
    assert old["version"] != new["version"]
    assert v2.clock(state["armed"]["frozen_at"]) > v2.clock(new["created_at"])


def test_nearest_obstacle_is_not_skipped_for_a_more_profitable_distant_target():
    plan,state=through(29)
    reading=chart(30)
    r=reading.readings[-1]
    obstacle=pivot("LOW",98,-10,20)
    reading=replace(reading,readings=(*reading.readings[:-1],replace(r,pivot_lows=(obstacle,*r.pivot_lows))))
    state=observe(plan,state,30,reading=reading)
    assert state["stage"] == "INVALIDATED"
    assert state["reason"] == "INSUFFICIENT_SPACE_AT_TRIGGER_FREEZE"
    assert state["armed"] is None


def test_latest_pivot_already_broken_cancels_instead_of_falling_back_to_older_one():
    plan,state=through(29)
    state=observe(plan,state,30,reading=chart(30,extra=pivot("LOW",104,10,30)))
    assert state["reason"] == "BREAK_PRECEDED_TRIGGER_FREEZE"
    assert state["candidate"] is None


def test_no_leg_pivot_waits_without_using_pre_zone_pivots():
    reader=lambda i:chart(i,without_leg=True,new_confirmed=90)
    plan,state=through(30,reader=reader)
    assert state["stage"] == "WAIT_LOCAL_PIVOT" and state["stop"] == 106.5
    assert state["armed"] is None


def test_delayed_pivot_confirmation_cannot_turn_an_already_closed_break_into_entry():
    reader=lambda i:chart(i,without_leg=True,new_confirmed=45)
    plan,state=through(45,reader=reader)
    assert state["reason"] == "BREAK_PRECEDED_TRIGGER_FREEZE"


def test_delayed_confirmation_can_arm_but_requires_a_later_closed_break():
    plan,state=seeded()
    seen=[]
    for index in range(1,62):
        state=observe(plan,state,index,reading=chart(index,without_leg=True,new_confirmed=45),
            bar=candle(45,tf="15m",close=103,high=104,low=100) if 45<=index<60 else None)
        if index in (30,45,60,61):seen.append(state["stage"])
    assert seen == ["WAIT_LOCAL_PIVOT","WAIT_BREAK","WAIT_RECHECK","CANDIDATE_ONLY"]
    assert state["armed"]["frozen_at"] == (START+timedelta(minutes=45,seconds=2)).isoformat()


def test_pivot_formed_after_rejection_is_not_used_to_chase_another_leg():
    reader=lambda i:chart(i,without_leg=True,new_confirmed=90,
        extra=pivot("LOW",96,31,45) if i>=45 else None)
    plan,state=through(45,reader=reader)
    assert state["stage"] == "WAIT_LOCAL_PIVOT"


def test_future_pivot_confirmation_is_rejected():
    with pytest.raises(ValueError,match="future pivot"):
        v2.create_plan(chart(0,extra=pivot("LOW",99,-10,1)),100)


def test_armed_levels_never_move_when_a_new_reading_has_other_pivots():
    plan,state=through(30)
    frozen=copy.deepcopy(state["armed"])
    for index in range(31,46):
        state=observe(plan,state,index,reading=chart(index,extra=pivot("LOW",100,10,30)))
    assert state["armed"] == frozen and state["stage"] == "WAIT_RECHECK"
    again=observe(plan,state,45,reading=chart(45,extra=pivot("LOW",100,10,30)))
    assert again["stage"] == "WAIT_RECHECK" and again["candidate"] is None


@pytest.mark.parametrize("side",["SHORT","LONG"])
def test_failed_next_minute_recheck_cancels(side):
    plan,state=through(45,side=side)
    state=observe(plan,state,46,minute=candle(46,close=100,low=99.9,high=100.1,side=side))
    assert state["reason"] == "BREAKOUT_NOT_HELD_AT_RECHECK"


def test_recheck_recalculates_costs_at_current_price_instead_of_using_arming_quote():
    plan,state=through(45)
    state=observe(plan,state,46,minute=candle(46,close=91,low=90.9,high=91.1))
    assert state["reason"] == "INSUFFICIENT_SPACE_AFTER_PLANNED_COSTS"


@pytest.mark.parametrize("kwargs,reason",[
    ({"low":77},"TARGET_TOUCHED_BEFORE_PROPOSAL"),
    ({"high":107},"FROZEN_STOP_LEVEL_TOUCHED_BEFORE_PROPOSAL")])
def test_target_or_stop_wick_cancels_even_if_close_looks_valid(kwargs,reason):
    plan,state=through(45)
    state=observe(plan,state,46,minute=candle(46,**kwargs))
    assert state["stage"] == "INVALIDATED" and state["reason"] == reason


def test_missing_minute_changed_candle_and_armed_level_mutation_are_refused():
    plan,state=through(30)
    with pytest.raises(ValueError,match="Minute observation gap"):
        observe(plan,state,32)
    with pytest.raises(ValueError,match="Changed or reordered"):
        observe(plan,state,30,minute=candle(30,close=103.01))
    state["armed"]["target"] -= 1
    with pytest.raises(ValueError,match="Altered or future"):
        observe(plan,state,31)


def test_terminal_scenario_is_consumed_once_without_rearming():
    plan,state=through(46)
    later=observe(plan,state,47)
    assert later["stage"] == "CANDIDATE_ONLY" and later["candidate"] == state["candidate"]


def test_zone_plan_mutation_is_refused():
    plan,state=seeded()
    plan["zone_high"] += 1
    with pytest.raises(ValueError,match="Altered"):
        observe(plan,state,1)


def test_deadline_and_higher_context_changes_cancel_before_any_candidate():
    plan,_=v2.create_plan(chart(0),100,v2.Policy(lifetime_15m_bars=2))
    state=v2.initial_state(plan)
    for index in range(1,31):state=observe(plan,state,index)
    assert state["stage"] == "EXPIRED" and state["candidate"] is None
    plan,state=seeded()
    reading=chart(1)
    reading=replace(reading,readings=tuple(replace(r,regime="RANGE") for r in reading.readings))
    assert observe(plan,state,1,reading=reading)["reason"] == "HIGHER_TIMEFRAME_CONTEXT_CHANGED"


def test_stop_and_break_inside_same_closed_bar_cannot_be_interpreted_favorably():
    plan,state=through(44)
    state=observe(plan,state,45,bar=candle(45,tf="15m",high=107))
    assert state["reason"] == "AMBIGUOUS_STOP_AND_TRIGGER_BAR"

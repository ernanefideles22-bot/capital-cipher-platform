from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.market import Candle
from app.strategy import gate_scenario_observer as o
from app.strategy.professional_gate_chart_stage1 import (
    ConfirmedPivot, MultiTimeframeReading, ProfessionalChartReading,
)

START = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def bar(at, *, high=102, low=99, close=100, tf="15m", opened=None):
    return Candle(exchange="GATE", symbol="BTCUSDT", timeframe=tf,
        open=close if opened is None else opened, high=high, low=low, close=close,
        volume=100, closed_at=at, received_at=at + timedelta(seconds=1))


def context(at, last15=None, side="SHORT"):
    now = at + timedelta(seconds=2)
    transform = (lambda p: p) if side == "SHORT" else (lambda p: 200-p)
    def pivot(kind, price, i):
        return ConfirmedPivot(kind, i, transform(price), START-timedelta(hours=3-i/4),
                              START-timedelta(hours=2-i/4))
    highs = (pivot("HIGH", 120, 0), pivot("HIGH", 105, 1))
    lows = (pivot("LOW", 70, 0), pivot("LOW", 99, 1))
    if side == "LONG":
        highs, lows = tuple(replace(p, kind="HIGH") for p in lows), tuple(replace(p, kind="LOW") for p in highs)
    readings = tuple(ProfessionalChartReading(
        exchange="GATE", symbol="BTCUSDT", timeframe=tf, as_of=now,
        last_close_at=last15 or at, candles_analyzed=100,
        regime="DOWNTREND" if side == "SHORT" else "UPTREND",
        ema20=101, ema50=102, atr14=5, relative_volume=1,
        rsi14=50, macd_histogram=0, candle_patterns=(), chart_patterns=(),
        market_structure="LOWER_HIGHS_LOWER_LOWS" if side == "SHORT" else "HIGHER_HIGHS_HIGHER_LOWS",
        pivot_highs=highs, pivot_lows=lows, support=99, resistance=105,
        support_trend_slope_atr_per_bar=None, resistance_trend_slope_atr_per_bar=None,
        fibonacci_leg="DOWNSWING", fibonacci_retracements=(("0.5", 100),), breakout="NONE",
    ) for tf in ("4h", "1h", "15m"))
    return MultiTimeframeReading("GATE", "BTCUSDT", now, "MIXED_OR_UNCONFIRMED", readings)


def seed(side="SHORT", policy=o.Policy()):
    plan, _ = o.create_plan(context(START, side=side), 100, policy)
    return plan, o.initial_state(plan)


def step(plan, state, minutes, *, high, low, close, minute_close=None, new15=True, changed_context=False):
    at = START + timedelta(minutes=minutes)
    if plan["side"] == "LONG":
        high, low, close = 200-low, 200-high, 200-close
        minute_close = 200-minute_close if minute_close is not None else None
    last15 = at if new15 else o.clock(state["last_15m_close_at"])
    # Reuse immutable last-15m data from the previous call for a minute-only recheck.
    b = bar(last15, high=high, low=low, close=close)
    m = bar(at, tf="1m", high=max(close, minute_close or close)+.1,
            low=min(close, minute_close or close)-.1, close=minute_close if minute_close is not None else close)
    chart = context(at, last15, plan["side"])
    if changed_context:
        chart = replace(chart, readings=tuple(replace(r, regime="RANGE") for r in chart.readings))
    return o.advance(plan, state, chart, b, m, as_of=at+timedelta(seconds=2))


def sequence(side="SHORT"):
    plan, state = seed(side)
    states = [state]
    # Core state-machine unit tests supply only decision boundaries. The public
    # adapter/journal integration tests additionally supply every intervening minute.
    for n, vals in [(15,(106,103,105)), (30,(105.5,101,103)), (45,(103.5,97,98))]:
        state = {**state, "last_minute_close_at": None, "last_minute_hash": None}
        state = step(plan, state, n, high=vals[0], low=vals[1], close=vals[2])
        states.append(state)
    state = step(plan, state, 46, high=103.5, low=97, close=98, minute_close=97.9, new15=False)
    states.append(state)
    return plan, states


@pytest.mark.parametrize("side", ["SHORT", "LONG"])
def test_touch_rejection_break_then_next_minute_is_one_nonexecutable_candidate(side):
    plan, states = sequence(side)
    assert [s["stage"] for s in states] == ["WAIT_TOUCH", "WAIT_REJECTION", "WAIT_BREAK", "WAIT_RECHECK", "CANDIDATE_ONLY"]
    candidate = states[-1]["candidate"]
    assert candidate["side"] == side
    assert candidate["net_reward_risk"] >= 2
    assert not candidate["execution_authorized"] and not candidate["profitability_proven"]
    assert candidate["quantity_and_lot_rules_verified"] is False
    assert states[-1]["stop"] == pytest.approx(106.5 if side == "SHORT" else 93.5)
    assert o.validate_plan(plan) is None


def test_unchanged_price_waits_and_near_zone_is_not_a_trigger():
    plan, state = seed()
    value = step(plan, state, 1, high=102, low=99, close=100, new15=False, minute_close=103.8)
    assert value["stage"] == "WAIT_TOUCH" and value["near_zone"]
    assert value["candidate"] is None


def test_touch_and_break_same_bar_cannot_be_used_as_a_completed_sequence():
    plan, state = seed()
    value = step(plan, state, 15, high=106, low=97, close=98)
    assert value["stage"] == "INVALIDATED"
    assert value["reason"] == "TOUCH_AND_TRIGGER_SAME_BAR_ORDER_UNKNOWN"


def test_break_before_rejection_is_cancelled_instead_of_retroactively_confirmed():
    plan, state = seed()
    state = step(plan, state, 15, high=106, low=103, close=105)
    state["last_minute_close_at"] = None
    value = step(plan, state, 30, high=105.5, low=97, close=98)
    assert value["reason"] == "TRIGGER_CROSSED_BEFORE_SEPARATE_REJECTION"


def test_context_change_and_zone_invalidation_cancel_without_proposal():
    plan, state = seed()
    assert step(plan, state, 1, high=102, low=99, close=100, new15=False, changed_context=True)["reason"] == "HIGHER_TIMEFRAME_CONTEXT_CHANGED"
    assert step(plan, state, 15, high=108, low=103, close=107)["reason"] == "MINUTE_CLOSE_INVALIDATED_ZONE"


def test_stop_and_break_inside_same_bar_are_not_interpreted_favorably():
    plan, states = sequence()
    state = {**states[2], "last_minute_close_at": None}
    value = step(plan, state, 45, high=107, low=97, close=98, minute_close=98)
    assert value["stage"] == "INVALIDATED"
    assert value["reason"] == "AMBIGUOUS_STOP_AND_TRIGGER_BAR"


@pytest.mark.parametrize("price,reason", [(99.2,"BREAKOUT_NOT_HELD_AT_RECHECK"), (69,"TARGET_ALREADY_REACHED_BEFORE_PROPOSAL"), (90,"INSUFFICIENT_SPACE_AFTER_PLANNED_COSTS")])
def test_recheck_can_reject_return_inside_trigger_exhausted_reward_and_entry_drift(price,reason):
    plan, states = sequence()
    value = step(plan, states[3], 46, high=103.5, low=97, close=98, minute_close=price, new15=False)
    assert value["stage"] == "INVALIDATED" and value["reason"] == reason


def test_deadline_without_trigger_expires():
    plan, state = seed(policy=o.Policy(lifetime_15m_bars=1))
    value = step(plan, state, 15, high=102, low=99, close=100)
    assert value["stage"] == "EXPIRED"


def test_terminal_scenario_never_rearms():
    plan, states = sequence()
    later = step(plan, states[-1], 47, high=103.5, low=97, close=98, minute_close=98, new15=False)
    assert later["candidate"] == states[-1]["candidate"]
    assert later["stage"] == "CANDIDATE_ONLY"


def test_planned_costs_reduce_reward_and_increase_loss_budget():
    plan, _ = seed()
    result = o.economics(plan, 98, 106.5)
    assert result["net_reward_per_base_unit"] < 98-plan["target"]
    assert result["net_risk_per_base_unit"] > 106.5-98


def test_no_compatible_context_returns_wait_instead_of_forcing_an_entry():
    chart = context(START)
    chart = replace(chart, readings=tuple(replace(r, regime="RANGE") for r in chart.readings))
    assert o.create_plan(chart, 100) == (None, "WAIT_HIGHER_TIMEFRAME_STRUCTURE")


def test_levels_are_frozen_and_not_the_screenshot_price():
    plan, _ = seed()
    assert (plan["zone_low"],plan["zone_high"],plan["trigger"],plan["target"]) == (104,106,99,70)
    altered = {**plan, "trigger":98}
    with pytest.raises(ValueError,match="Altered"):
        o.initial_state(altered)
    ann = plan["initial_technical_annotations"]["4h"]
    assert ann["fibonacci_levels"] and ann["resistance_line_confirmed_anchors"]


def test_future_confirmation_pivot_is_rejected():
    chart = context(START)
    frame = chart.readings[-1]
    bad = replace(frame.pivot_highs[-1], confirmed_at=START+timedelta(hours=1))
    chart = replace(chart, readings=(*chart.readings[:-1],replace(frame,pivot_highs=(bad,))))
    with pytest.raises(ValueError,match="future pivot"):
        o.create_plan(chart,100)


def test_forged_geometry_is_refused_even_with_recomputed_identity():
    plan,_=seed()
    plan["target"]=110
    plan["scenario_id"]=o.digest({k:v for k,v in plan.items() if k!="scenario_id"})
    with pytest.raises(ValueError,match="geometry"):
        o.validate_plan(plan)


@pytest.mark.parametrize("kw", [{"zone_atr":float('nan')},{"fee_bps_side":-1},{"slippage_bps_side":101},{"lifetime_15m_bars":True},{"minimum_net_reward_risk":0}])
def test_policy_refuses_nonfinite_or_unbounded_inputs(kw):
    with pytest.raises(ValueError):
        o.Policy(**kw)


def test_missing_minute_cannot_be_silently_reconstructed():
    plan, states = sequence()
    with pytest.raises(ValueError,match="Minute observation gap"):
        step(plan, states[3], 47, high=103.5, low=97, close=98, new15=False)


def test_future_or_stale_minute_is_rejected():
    b=bar(START,tf="1m")
    with pytest.raises(ValueError,match="Fresh"):
        o._minute(b,"BTCUSDT",START)
    with pytest.raises(ValueError,match="Fresh"):
        o._minute(b,"BTCUSDT",START+timedelta(minutes=3))

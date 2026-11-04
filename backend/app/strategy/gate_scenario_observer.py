"""Explainable, orderless scenario tracking over the existing causal S1 reader.

One frozen scenario; no indicator vote, confidence score or profit claim.
The caller must retain the returned state and supply every new closed 15m bar.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.professional_gate_chart_stage1 import (
    MultiTimeframeReading, analyze_gate_multi_timeframe,
)

VERSION = "GATE_CONDITIONAL_SCENARIO_OBSERVER_V1"
TERMINAL = {"CANDIDATE_ONLY", "INVALIDATED", "EXPIRED"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda v: v.isoformat() if isinstance(v, datetime) else _unsupported(v))


def _unsupported(value):
    raise TypeError(f"Unsupported scenario value: {type(value).__name__}")


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clock(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware observation clock required")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Policy:
    zone_atr: float = .2
    stop_buffer_atr: float = .1
    lifetime_15m_bars: int = 8
    recheck_minutes: int = 2
    fee_bps_side: float = 5.
    slippage_bps_side: float = 6.
    minimum_net_reward_risk: float = 2.

    def __post_init__(self):
        values = (self.zone_atr, self.stop_buffer_atr, self.fee_bps_side,
                  self.slippage_bps_side, self.minimum_net_reward_risk)
        if (any(type(v) not in (int, float) or not isfinite(v) for v in values)
                or not 0 < self.zone_atr <= 1 or not 0 < self.stop_buffer_atr <= 1
                or not 0 <= self.fee_bps_side <= 100
                or not 0 <= self.slippage_bps_side <= 100
                or not 1 <= self.minimum_net_reward_risk <= 10
                or type(self.lifetime_15m_bars) is not int or not 1 <= self.lifetime_15m_bars <= 32
                or type(self.recheck_minutes) is not int or not 1 <= self.recheck_minutes <= 5):
            raise ValueError("Invalid bounded scenario policy")


def _frames(reading):
    values = {r.timeframe: r for r in reading.readings}
    if (reading.exchange != "GATE" or reading.trading_authorized is not False
            or len(reading.readings) != 3 or set(values) != {"4h", "1h", "15m"}):
        raise ValueError("Complete non-executable S1 context required")
    for r in values.values():
        if (r.symbol != reading.symbol or r.exchange != "GATE"
                or r.trading_authorized is not False or r.predictive_edge_proven is not False
                or clock(r.as_of) != clock(reading.as_of)
                or clock(r.last_close_at) > clock(r.as_of)):
            raise ValueError("Mixed or future S1 context")
        if any(clock(p.confirmed_at) > clock(reading.as_of) for p in r.pivot_highs + r.pivot_lows):
            raise ValueError("Unconfirmed future pivot")
    return values


def bias(reading):
    frames = _frames(reading)
    higher, middle = frames["4h"], frames["1h"]
    if (higher.market_structure == "LOWER_HIGHS_LOWER_LOWS" and higher.regime == "DOWNTREND"
            and middle.regime != "UPTREND"):
        return "SHORT"
    if (higher.market_structure == "HIGHER_HIGHS_HIGHER_LOWS" and higher.regime == "UPTREND"
            and middle.regime != "DOWNTREND"):
        return "LONG"
    return None


def annotations(reading):
    """Expose actual S1 evidence, including line anchors, without inventing votes."""
    frames = _frames(reading)
    result = {}
    for tf, r in frames.items():
        result[tf] = {"regime": r.regime, "structure": r.market_structure,
            "candle_patterns": r.candle_patterns, "chart_patterns": r.chart_patterns,
            "support": r.support, "resistance": r.resistance,
            "support_line_confirmed_anchors": [asdict(p) for p in r.pivot_lows[-2:]],
            "resistance_line_confirmed_anchors": [asdict(p) for p in r.pivot_highs[-2:]],
            "fibonacci_leg": r.fibonacci_leg, "fibonacci_levels": r.fibonacci_retracements,
            "atr14": r.atr14, "relative_volume": r.relative_volume,
            "rsi14": r.rsi14, "macd_histogram": r.macd_histogram}
    return result


def create_plan(reading, reference_price, policy=Policy()):
    """Freeze levels BEFORE the future touch; never hardcode screenshot prices."""
    frames = _frames(reading)
    side, short = bias(reading), frames["15m"]
    if side is None:
        return None, "WAIT_HIGHER_TIMEFRAME_STRUCTURE"
    price, atr = float(reference_price), short.atr14
    if not isfinite(price) or not isfinite(atr) or min(price, atr) <= 0:
        raise ValueError("Positive finite reference price and ATR required")
    sign = 1 if side == "LONG" else -1
    zone_pivots = short.pivot_lows if side == "LONG" else short.pivot_highs
    available = [p for p in zone_pivots if sign * (price - p.price) > policy.zone_atr * atr]
    if not available:
        return None, "WAIT_CONFIRMED_LEVEL_AHEAD_OF_PRICE"
    anchor = min(available, key=lambda p: abs(price - p.price))
    triggers = short.pivot_highs if side == "LONG" else short.pivot_lows
    triggers = [p for p in triggers if sign * (p.price - price) > 0]
    if not triggers:
        return None, "WAIT_CONFIRMED_LOCAL_TRIGGER"
    trigger = max(triggers, key=lambda p: p.occurred_at)
    targets = [p for r in frames.values() for p in
               (r.pivot_highs if side == "LONG" else r.pivot_lows)
               if sign * (p.price - trigger.price) > 0]
    if not targets:
        return None, "WAIT_STRUCTURAL_TARGET_BEYOND_TRIGGER"
    target = min(targets, key=lambda p: abs(p.price - trigger.price))
    now = clock(reading.as_of)
    plan = {"version": VERSION, "symbol": reading.symbol, "side": side,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15 * policy.lifetime_15m_bars)).isoformat(),
        "seed_15m_close_at": clock(short.last_close_at).isoformat(),
        "zone_low": anchor.price - policy.zone_atr * atr,
        "zone_high": anchor.price + policy.zone_atr * atr,
        "zone_anchor": asdict(anchor), "trigger": trigger.price,
        "trigger_anchor": asdict(trigger), "target": target.price, "target_anchor": asdict(target),
        "stop_buffer": policy.stop_buffer_atr * atr, "policy": asdict(policy),
        "initial_technical_annotations": annotations(reading),
        "execution_authorized": False, "predictive_edge_proven": False}
    # Normalize times to strings for stable durable state and exact restoration.
    plan = json.loads(canonical(plan))
    plan["scenario_id"] = digest(plan)
    return plan, "SCENARIO_FROZEN_BEFORE_TOUCH"


def validate_plan(plan):
    clean = {k: v for k, v in plan.items() if k != "scenario_id"}
    if (plan["scenario_id"] != digest(clean) or plan["version"] != VERSION
            or plan["side"] not in {"LONG", "SHORT"}
            or plan["execution_authorized"] is not False
            or plan["predictive_edge_proven"] is not False):
        raise ValueError("Altered or executable scenario plan")
    Policy(**plan["policy"])
    levels = (plan["zone_low"], plan["zone_high"], plan["trigger"], plan["target"], plan["stop_buffer"])
    sign = 1 if plan["side"] == "LONG" else -1
    near = plan["zone_high"] if sign == 1 else plan["zone_low"]
    if (any(type(v) not in (int, float) or not isfinite(v) or v <= 0 for v in levels)
            or plan["zone_low"] >= plan["zone_high"]
            or sign * (plan["trigger"] - near) <= 0
            or sign * (plan["target"] - plan["trigger"]) <= 0
            or clock(plan["seed_15m_close_at"]) > clock(plan["created_at"])
            or clock(plan["expires_at"]) - clock(plan["created_at"]) !=
                timedelta(minutes=15 * plan["policy"]["lifetime_15m_bars"])):
        raise ValueError("Invalid frozen level geometry or deadline")


def initial_state(plan):
    validate_plan(plan)
    return {"stage": "WAIT_TOUCH", "reason": "WAIT_FROZEN_ZONE", "scenario_id": plan["scenario_id"],
        "last_15m_close_at": plan["seed_15m_close_at"], "last_15m_hash": None,
        "last_minute_close_at": None, "last_minute_hash": None,
        "last_observed_at": plan["created_at"], "touch_extreme": None, "stop": None,
        "triggered_at": None, "triggered_bar_close_at": None,
        "candidate": None, "near_zone": False, "execution_authorized": False}


def _minute(bar, symbol, now):
    if (bar.exchange != Exchange.GATE or bar.symbol != symbol or bar.timeframe != "1m"
            or clock(bar.received_at) < clock(bar.closed_at)
            or clock(bar.received_at) > now or clock(bar.closed_at) > now
            or now - clock(bar.closed_at) > timedelta(minutes=2)
            or not all(isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close, bar.volume))
            or min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)):
        raise ValueError("Fresh closed and received 1m candle required")


def economics(plan, reference, stop):
    cfg = plan["policy"]
    sign = 1 if plan["side"] == "LONG" else -1
    slip, fee = cfg["slippage_bps_side"] / 10000, cfg["fee_bps_side"] / 10000
    entry = reference * (1 + sign * slip)
    target_exit = plan["target"] * (1 - sign * slip)
    stop_exit = stop * (1 - sign * slip)
    reward = sign * (target_exit - entry) - fee * (entry + target_exit)
    risk = sign * (entry - stop_exit) + fee * (entry + stop_exit)
    return {"reference_price_not_fill": reference, "stop_reference": stop,
        "target_reference": plan["target"], "net_reward_per_base_unit": reward,
        "net_risk_per_base_unit": risk, "net_reward_risk": reward / risk if risk > 0 else None,
        "funding_not_yet_modelled": True, "historical_fills_verified": False,
        "account_fees_verified": False, "quantity_and_lot_rules_verified": False}


def advance(plan, state, reading, bar15, minute, *, as_of):
    """Observe one fresh context. Trigger and recheck MUST be separate observations."""
    validate_plan(plan)
    now, frames = clock(as_of), _frames(reading)
    if (reading.symbol != plan["symbol"] or clock(reading.as_of) != now
            or state["scenario_id"] != plan["scenario_id"]
            or state["execution_authorized"] is not False
            or state["stage"] not in TERMINAL | {"WAIT_TOUCH", "WAIT_REJECTION", "WAIT_BREAK", "WAIT_RECHECK"}
            or now < clock(state["last_observed_at"])):
        raise ValueError("Scenario identity, context or clock mismatch")
    _minute(minute, plan["symbol"], now)
    if (bar15.exchange != Exchange.GATE or bar15.symbol != plan["symbol"] or bar15.timeframe != "15m"
            or clock(bar15.closed_at) != clock(frames["15m"].last_close_at)
            or clock(bar15.received_at) > now or clock(bar15.received_at) < clock(bar15.closed_at)
            or clock(minute.closed_at) < clock(bar15.closed_at)):
        raise ValueError("15m source does not match current S1 reading")
    result = json.loads(canonical(state))
    prev15 = clock(state["last_15m_close_at"])
    last15, mtime = clock(bar15.closed_at), clock(minute.closed_at)
    h15, hm = digest(bar15.model_dump(mode="json")), digest(minute.model_dump(mode="json"))
    if last15 < prev15 or last15 - prev15 > timedelta(minutes=15):
        raise ValueError("Reordered or unprocessed 15m observations")
    if last15 == prev15 and state["last_15m_hash"] not in {None, h15}:
        raise ValueError("Previously observed closed 15m candle changed")
    previous_minute = state["last_minute_close_at"]
    if previous_minute:
        prior_minute = clock(previous_minute)
        if mtime < prior_minute or (mtime == prior_minute and state["last_minute_hash"] != hm):
            raise ValueError("Changed or reordered minute candle")
        if mtime - prior_minute > timedelta(minutes=1):
            raise ValueError("Minute observation gap; continuity review required")
    result.update(last_15m_close_at=last15.isoformat(), last_15m_hash=h15,
                  last_minute_close_at=mtime.isoformat(), last_minute_hash=hm,
                  last_observed_at=now.isoformat())
    result["near_zone"] = plan["zone_low"] - plan["stop_buffer"] <= minute.close <= plan["zone_high"] + plan["stop_buffer"]
    if state["stage"] in TERMINAL:
        return result  # Never automatically rearm a consumed/rejected scenario.
    if now >= clock(plan["expires_at"]):
        result.update(stage="EXPIRED", reason="SCENARIO_DEADLINE")
        return result
    if bias(reading) != plan["side"]:
        result.update(stage="INVALIDATED", reason="HIGHER_TIMEFRAME_CONTEXT_CHANGED")
        return result
    sign = 1 if plan["side"] == "LONG" else -1
    beyond_zone = minute.close < plan["zone_low"] if sign == 1 else minute.close > plan["zone_high"]
    if beyond_zone:
        result.update(stage="INVALIDATED", reason="MINUTE_CLOSE_INVALIDATED_ZONE")
        return result
    if state["stop"] is not None and ((sign == 1 and minute.low <= state["stop"])
                                        or (sign == -1 and minute.high >= state["stop"])):
        result.update(stage="INVALIDATED", reason="FROZEN_STOP_LEVEL_TOUCHED_BEFORE_PROPOSAL")
        return result
    if state["stage"] == "WAIT_RECHECK":
        if now - clock(state["triggered_at"]) > timedelta(minutes=plan["policy"]["recheck_minutes"]):
            result.update(stage="EXPIRED", reason="PRE_ENTRY_RECHECK_DEADLINE")
        elif (mtime > clock(state["triggered_bar_close_at"])
              and clock(minute.received_at) > clock(state["triggered_at"])):
            if sign * (minute.close - plan["trigger"]) <= 0:
                result.update(stage="INVALIDATED", reason="BREAKOUT_NOT_HELD_AT_RECHECK")
            elif sign * (plan["target"] - minute.close) <= 0:
                result.update(stage="INVALIDATED", reason="TARGET_ALREADY_REACHED_BEFORE_PROPOSAL")
            else:
                value = economics(plan, minute.close, state["stop"])
                if (value["net_risk_per_base_unit"] <= 0 or value["net_reward_per_base_unit"] <= 0
                        or value["net_reward_risk"] < plan["policy"]["minimum_net_reward_risk"]):
                    result.update(stage="INVALIDATED", reason="INSUFFICIENT_SPACE_AFTER_PLANNED_COSTS")
                else:
                    result.update(stage="CANDIDATE_ONLY", reason="SEQUENCE_AND_NEXT_MINUTE_RECHECK_CONFIRMED",
                        candidate={**value, "side": plan["side"], "observed_at": now.isoformat(),
                            "proposal_id": digest([plan["scenario_id"], state["triggered_bar_close_at"]]),
                            "execution_authorized": False, "profitability_proven": False})
        return result
    if last15 == prev15:
        return result
    if (state["stop"] is not None and ((sign == 1 and bar15.low <= state["stop"])
                                       or (sign == -1 and bar15.high >= state["stop"]))):
        result.update(stage="INVALIDATED", reason="AMBIGUOUS_STOP_AND_TRIGGER_BAR")
        return result
    touched = bar15.low <= plan["zone_high"] and bar15.high >= plan["zone_low"]
    crossed = sign * (bar15.close - plan["trigger"]) > 0
    if state["stage"] == "WAIT_TOUCH":
        if touched:
            if crossed:
                result.update(stage="INVALIDATED", reason="TOUCH_AND_TRIGGER_SAME_BAR_ORDER_UNKNOWN")
            else:
                result.update(stage="WAIT_REJECTION", reason="ZONE_TOUCHED_WAIT_SEPARATE_REJECTION",
                              touch_extreme=bar15.low if sign == 1 else bar15.high)
    elif state["stage"] == "WAIT_REJECTION":
        extreme = min(state["touch_extreme"], bar15.low) if sign == 1 else max(state["touch_extreme"], bar15.high)
        result["touch_extreme"] = extreme
        rejected = bar15.close > plan["zone_high"] if sign == 1 else bar15.close < plan["zone_low"]
        if crossed:
            result.update(stage="INVALIDATED", reason="TRIGGER_CROSSED_BEFORE_SEPARATE_REJECTION")
        elif rejected:
            result.update(stage="WAIT_BREAK", reason="REJECTION_CONFIRMED_WAIT_LOCAL_BREAK",
                          stop=extreme - sign * plan["stop_buffer"])
    elif state["stage"] == "WAIT_BREAK" and crossed:
        if sign * (plan["target"] - bar15.close) <= 0:
            result.update(stage="INVALIDATED", reason="TARGET_ALREADY_PASSED_ON_TRIGGER_BAR")
        else:
            result.update(stage="WAIT_RECHECK", reason="LOCAL_BREAK_CLOSED_WAIT_NEXT_MINUTE",
                          triggered_at=now.isoformat(), triggered_bar_close_at=last15.isoformat())
    return result


def analyze_snapshot(payload):
    """Validated public-data adapter; snapshots carry actual supplied receipt times."""
    now = clock(payload["as_of"])
    frames = {tf: tuple(Candle.model_validate(v) for v in bars)
              for tf, bars in payload["candles"].items()}
    reading = analyze_gate_multi_timeframe(frames, as_of=now)
    minute = Candle.model_validate(payload["minute"])
    _minute(minute, reading.symbol, now)
    return reading, frames["15m"][-1], minute, now

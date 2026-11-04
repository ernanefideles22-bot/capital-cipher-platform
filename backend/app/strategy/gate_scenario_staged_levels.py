"""Research V2: freeze the zone first, then the causal rejection-leg trigger.

Independent from V1 and its financial study. No broker or execution authority.
The durable adapter retains every input; this core only transforms supplied state.
"""
from dataclasses import asdict
from datetime import timedelta
import json
from math import isfinite

from app.schemas.common import Exchange
from app.strategy import gate_scenario_observer as v1

Policy = v1.Policy
canonical, digest, clock = v1.canonical, v1.digest, v1.clock
annotations, analyze_snapshot = v1.annotations, v1.analyze_snapshot
TERMINAL = v1.TERMINAL
VERSION = "GATE_CONDITIONAL_SCENARIO_STAGED_LEVELS_V2"


def create_plan(reading, reference_price, policy=Policy()):
    frames = v1._frames(reading)
    side, short = v1.bias(reading), frames["15m"]
    if side is None:
        return None, "WAIT_HIGHER_TIMEFRAME_STRUCTURE"
    price, atr = float(reference_price), short.atr14
    if not all(isfinite(x) and x > 0 for x in (price, atr)):
        raise ValueError("Positive finite reference price and ATR required")
    sign = 1 if side == "LONG" else -1
    pivots = short.pivot_lows if sign == 1 else short.pivot_highs
    available = [p for p in pivots if sign * (price-p.price) > policy.zone_atr*atr]
    if not available:
        return None, "WAIT_CONFIRMED_LEVEL_AHEAD_OF_PRICE"
    anchor = min(available, key=lambda p: (abs(price-p.price), -clock(p.occurred_at).timestamp()))
    now = clock(reading.as_of)
    plan = {"version": VERSION, "symbol": reading.symbol, "side": side,
        "created_at": now, "expires_at": now+timedelta(minutes=15*policy.lifetime_15m_bars),
        "seed_15m_close_at": short.last_close_at, "zone_anchor": asdict(anchor),
        "zone_low": anchor.price-policy.zone_atr*atr,
        "zone_high": anchor.price+policy.zone_atr*atr,
        "stop_buffer": policy.stop_buffer_atr*atr, "policy": asdict(policy),
        "initial_technical_annotations": annotations(reading),
        "execution_authorized": False, "predictive_edge_proven": False}
    plan = json.loads(canonical(plan))
    plan["scenario_id"] = digest(plan)
    return plan, "ZONE_FROZEN_TRIGGER_NOT_YET_SELECTED"


def validate_plan(plan):
    if (plan["scenario_id"] != digest({k:v for k,v in plan.items() if k != "scenario_id"})
            or plan["version"] != VERSION or plan["side"] not in {"LONG", "SHORT"}
            or plan["execution_authorized"] is not False or plan["predictive_edge_proven"] is not False
            or "trigger" in plan or "target" in plan):
        raise ValueError("Altered or executable zone plan")
    Policy(**plan["policy"])
    levels = (plan["zone_low"], plan["zone_high"], plan["stop_buffer"])
    if (not all(type(x) in (int, float) and isfinite(x) and x > 0 for x in levels)
            or plan["zone_low"] >= plan["zone_high"]
            or clock(plan["zone_anchor"]["confirmed_at"]) > clock(plan["created_at"])
            or clock(plan["seed_15m_close_at"]) > clock(plan["created_at"])
            or clock(plan["expires_at"])-clock(plan["created_at"]) !=
                timedelta(minutes=15*plan["policy"]["lifetime_15m_bars"])):
        raise ValueError("Invalid frozen zone geometry or deadline")


def initial_state(plan):
    validate_plan(plan)
    return {"scenario_id": plan["scenario_id"], "stage": "WAIT_TOUCH", "reason": "WAIT_FROZEN_ZONE",
        "last_15m_close_at": plan["seed_15m_close_at"], "last_15m_hash": None,
        "last_minute_close_at": None, "last_minute_hash": None,
        "last_observed_at": plan["created_at"], "touch_extreme": None, "stop": None,
        "rejection_close_at": None, "unarmed_close_extreme": None,
        "unarmed_price_extreme": None, "armed": None,
        "triggered_at": None, "triggered_bar_close_at": None,
        "near_zone": False, "candidate": None, "execution_authorized": False}


def _arm(plan, state, reading, now):
    """Latest leg pivot first; never fall back to an older, more convenient one."""
    frames, sign = v1._frames(reading), 1 if plan["side"] == "LONG" else -1
    frame = frames["15m"]
    pool = frame.pivot_highs if sign == 1 else frame.pivot_lows
    pool = [p for p in pool if clock(plan["zone_anchor"]["occurred_at"]) < clock(p.occurred_at)
            <= clock(state["rejection_close_at"]) and clock(p.confirmed_at) <= now]
    if not pool:
        return None, "WAIT_CONFIRMED_REJECTION_LEG_PIVOT"
    trigger = max(pool, key=lambda p: (clock(p.occurred_at), clock(p.confirmed_at)))
    if sign*(state["unarmed_close_extreme"]-trigger.price) >= 0:
        return None, "BREAK_PRECEDED_TRIGGER_FREEZE"
    # First structural obstacle across ALL frames; a nearby 15m obstacle cannot be skipped.
    targets = [(tf, p) for tf, r in frames.items() for p in
        (r.pivot_highs if sign == 1 else r.pivot_lows) if sign*(p.price-trigger.price) > 0]
    if not targets:
        return None, "NO_STRUCTURAL_TARGET_BEYOND_LOCAL_TRIGGER"
    tf, target = min(targets, key=lambda x: (abs(x[1].price-trigger.price),
                                            x[0], clock(x[1].occurred_at)))
    if sign*(state["unarmed_price_extreme"]-target.price) >= 0:
        return None, "TARGET_TOUCHED_BEFORE_TRIGGER_FREEZE"
    armed = {"scenario_id": plan["scenario_id"], "frozen_at": now.isoformat(),
        "rejection_close_at": state["rejection_close_at"], "stop": state["stop"],
        "trigger": trigger.price, "trigger_anchor": asdict(trigger),
        "target": target.price, "target_anchor": asdict(target), "target_timeframe": tf}
    armed = json.loads(canonical(armed))
    quote = v1.economics({**plan, **armed}, trigger.price, state["stop"])
    if (sign*(trigger.price-state["stop"]) <= 0 or quote["net_risk_per_base_unit"] <= 0
            or quote["net_reward_per_base_unit"] <= 0
            or quote["net_reward_risk"] < plan["policy"]["minimum_net_reward_risk"]):
        return None, "INSUFFICIENT_SPACE_AT_TRIGGER_FREEZE"
    armed["economics_at_trigger_not_fill"] = quote
    armed["level_id"] = digest(armed)
    return armed, "LOCAL_TRIGGER_AND_FIRST_OBSTACLE_FROZEN"


def _checked(plan, state, reading, bar15, minute, now):
    validate_plan(plan)
    frames = v1._frames(reading)
    if (reading.symbol != plan["symbol"] or clock(reading.as_of) != now
            or state["scenario_id"] != plan["scenario_id"] or state["execution_authorized"] is not False
            or state["stage"] not in TERMINAL | {"WAIT_TOUCH", "WAIT_REJECTION", "WAIT_LOCAL_PIVOT", "WAIT_BREAK", "WAIT_RECHECK"}
            or now < clock(state["last_observed_at"])):
        raise ValueError("Scenario identity, context or clock mismatch")
    v1._minute(minute, plan["symbol"], now)
    if (bar15.exchange != Exchange.GATE or bar15.symbol != plan["symbol"] or bar15.timeframe != "15m"
            or clock(bar15.closed_at) != clock(frames["15m"].last_close_at)
            or not clock(bar15.closed_at) <= clock(bar15.received_at) <= now
            or clock(minute.closed_at) < clock(bar15.closed_at)):
        raise ValueError("15m source does not match current S1 reading")
    last15, prev15 = clock(bar15.closed_at), clock(state["last_15m_close_at"])
    h15, hm = digest(bar15.model_dump(mode="json")), digest(minute.model_dump(mode="json"))
    if not timedelta(0) <= last15-prev15 <= timedelta(minutes=15):
        raise ValueError("Reordered or unprocessed 15m observations")
    if last15 == prev15 and state["last_15m_hash"] not in {None, h15}:
        raise ValueError("Previously observed closed 15m candle changed")
    if state["last_minute_close_at"]:
        gap = clock(minute.closed_at)-clock(state["last_minute_close_at"])
        if gap < timedelta(0) or (gap == timedelta(0) and state["last_minute_hash"] != hm):
            raise ValueError("Changed or reordered minute candle")
        if gap > timedelta(minutes=1):
            raise ValueError("Minute observation gap; continuity review required")
    armed = state["armed"]
    if armed is not None and (armed["level_id"] != digest({k:v for k,v in armed.items() if k != "level_id"})
            or armed["scenario_id"] != plan["scenario_id"] or armed["stop"] != state["stop"]
            or clock(armed["frozen_at"]) > clock(state["last_observed_at"])):
        raise ValueError("Altered or future armed levels")
    if state["stage"] in {"WAIT_BREAK", "WAIT_RECHECK", "CANDIDATE_ONLY"} and armed is None:
        raise ValueError("Frozen local trigger required")
    result = json.loads(canonical(state))
    result.update(last_15m_close_at=last15.isoformat(), last_15m_hash=h15,
        last_minute_close_at=clock(minute.closed_at).isoformat(), last_minute_hash=hm,
        last_observed_at=now.isoformat())
    return result, last15 > prev15


def advance(plan, state, reading, bar15, minute, *, as_of):
    now = clock(as_of)
    result, new15 = _checked(plan, state, reading, bar15, minute, now)
    sign = 1 if plan["side"] == "LONG" else -1
    result["near_zone"] = plan["zone_low"]-plan["stop_buffer"] <= minute.close <= plan["zone_high"]+plan["stop_buffer"]

    def finish(stage, reason):
        result.update(stage=stage, reason=reason)
        return result

    if state["stage"] in TERMINAL:
        return result
    if now >= clock(plan["expires_at"]):
        return finish("EXPIRED", "SCENARIO_DEADLINE")
    if v1.bias(reading) != plan["side"]:
        return finish("INVALIDATED", "HIGHER_TIMEFRAME_CONTEXT_CHANGED")
    if (minute.close < plan["zone_low"] if sign == 1 else minute.close > plan["zone_high"]):
        return finish("INVALIDATED", "MINUTE_CLOSE_INVALIDATED_ZONE")
    if state["stop"] is not None and (minute.low <= state["stop"] if sign == 1 else minute.high >= state["stop"]):
        return finish("INVALIDATED", "FROZEN_STOP_LEVEL_TOUCHED_BEFORE_PROPOSAL")
    armed = state["armed"]
    if state["stage"] == "WAIT_LOCAL_PIVOT":
        result["unarmed_price_extreme"] = (max if sign == 1 else min)(
            state["unarmed_price_extreme"],minute.high if sign == 1 else minute.low)
    if armed is not None and (minute.high >= armed["target"] if sign == 1 else minute.low <= armed["target"]):
        return finish("INVALIDATED", "TARGET_TOUCHED_BEFORE_PROPOSAL")
    if state["stage"] == "WAIT_RECHECK":
        if now-clock(state["triggered_at"]) > timedelta(minutes=plan["policy"]["recheck_minutes"]):
            return finish("EXPIRED", "PRE_ENTRY_RECHECK_DEADLINE")
        if (clock(minute.closed_at) > clock(state["triggered_bar_close_at"])
                and clock(minute.received_at) > clock(state["triggered_at"])):
            if sign*(minute.close-armed["trigger"]) <= 0:
                return finish("INVALIDATED", "BREAKOUT_NOT_HELD_AT_RECHECK")
            quote = v1.economics({**plan, **armed}, minute.close, state["stop"])
            if (quote["net_risk_per_base_unit"] <= 0 or quote["net_reward_per_base_unit"] <= 0
                    or quote["net_reward_risk"] < plan["policy"]["minimum_net_reward_risk"]):
                return finish("INVALIDATED", "INSUFFICIENT_SPACE_AFTER_PLANNED_COSTS")
            result["candidate"] = {**quote, "side": plan["side"], "observed_at": now.isoformat(),
                "level_id": armed["level_id"], "proposal_id": digest([armed["level_id"],state["triggered_bar_close_at"]]),
                "execution_authorized": False, "profitability_proven": False}
            return finish("CANDIDATE_ONLY", "STAGED_LEVELS_AND_NEXT_MINUTE_RECHECK_CONFIRMED")
        return result
    if not new15:
        return result
    if state["stop"] is not None and (bar15.low <= state["stop"] if sign == 1 else bar15.high >= state["stop"]):
        return finish("INVALIDATED", "AMBIGUOUS_STOP_AND_TRIGGER_BAR")
    if state["stage"] == "WAIT_TOUCH":
        if bar15.low <= plan["zone_high"] and bar15.high >= plan["zone_low"]:
            result.update(touch_extreme=bar15.low if sign == 1 else bar15.high)
            return finish("WAIT_REJECTION", "ZONE_TOUCHED_WAIT_SEPARATE_REJECTION")
    elif state["stage"] == "WAIT_REJECTION":
        extreme = min(state["touch_extreme"], bar15.low) if sign == 1 else max(state["touch_extreme"], bar15.high)
        result["touch_extreme"] = extreme
        rejected = bar15.close > plan["zone_high"] if sign == 1 else bar15.close < plan["zone_low"]
        if rejected:
            result.update(stop=extreme-sign*plan["stop_buffer"],
                rejection_close_at=clock(bar15.closed_at).isoformat(), unarmed_close_extreme=bar15.close,
                unarmed_price_extreme=bar15.high if sign == 1 else bar15.low)
            armed, reason = _arm(plan, result, reading, now)
            result["armed"] = armed
            return finish("WAIT_BREAK" if armed else "WAIT_LOCAL_PIVOT" if reason.startswith("WAIT_") else "INVALIDATED", reason)
    elif state["stage"] == "WAIT_LOCAL_PIVOT":
        result["unarmed_close_extreme"] = (max if sign == 1 else min)(state["unarmed_close_extreme"],bar15.close)
        result["unarmed_price_extreme"] = (max if sign == 1 else min)(
            result["unarmed_price_extreme"],bar15.high if sign == 1 else bar15.low)
        armed, reason = _arm(plan, result, reading, now)
        result["armed"] = armed
        return finish("WAIT_BREAK" if armed else "WAIT_LOCAL_PIVOT" if reason.startswith("WAIT_") else "INVALIDATED", reason)
    elif state["stage"] == "WAIT_BREAK" and sign*(bar15.close-armed["trigger"]) > 0:
        if clock(bar15.received_at) <= clock(armed["frozen_at"]):
            return finish("INVALIDATED", "BREAK_PRECEDED_TRIGGER_FREEZE")
        if (bar15.high >= armed["target"] if sign == 1 else bar15.low <= armed["target"]):
            return finish("INVALIDATED", "TARGET_TOUCHED_ON_TRIGGER_BAR")
        result.update(triggered_at=now.isoformat(), triggered_bar_close_at=clock(bar15.closed_at).isoformat())
        return finish("WAIT_RECHECK", "LOCAL_BREAK_CLOSED_WAIT_NEXT_MINUTE")
    return result

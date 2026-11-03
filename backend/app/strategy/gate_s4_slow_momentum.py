"""One fixed research hypothesis: 20-day own-price trend on 4h candles."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta, timezone
from math import log, sqrt
from statistics import pstdev

from app.strategy.gate_classical_s2 import _atr, _check_bars
from app.strategy.gate_s4_complete_playbooks import Playbook, PlaybookDecision
from app.strategy.gate_s4_lab import digest

POLICY_ID = "TSMOM_20D_4H_COST_BUDGET_V1"
POLICY = Playbook(POLICY_ID, "1.0.0", "OWN_PRICE_20D_TREND", 30, 6, 3, 3., "CASH")
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
RULES = {"lookback_bars": 120, "volatility_bars": 120, "minimum_history": 220,
    "minimum_absolute_normalized_trend": 1., "decision_hour_utc": 0,
    "stop_atr": 2., "reward_to_risk": 3., "atr_period": 14,
    "maximum_entry_drift_atr": .5, "minimum_entry_risk_atr": .5,
    "maximum_entry_risk_atr": 3., "maximum_planned_cost_share_of_target": .20,
    "planning_fee_bps_side": 12., "planning_slippage_bps_side": 12.,
    "planning_funding_bps_8h": 2., "maximum_funding_periods": 15,
    "entry_lag_bars": 2, "invalidation_exit_lag_bars": 2}
SOURCES = ["https://www.nber.org/papers/w24877",
           "https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum"]


def planning_cost_fraction():
    # Fixed affordability proxy. Not a bound on realized costs, gaps or returns.
    return (2 * (RULES["planning_fee_bps_side"] + RULES["planning_slippage_bps_side"])
            + RULES["maximum_funding_periods"] * RULES["planning_funding_bps_8h"]) / 10000


def manifest():
    return {"policy": asdict(POLICY), "rules": dict(RULES), "symbols": list(SYMBOLS),
        "timeframe": "4h", "planning_cost_fraction": planning_cost_fraction(),
        "sources": list(SOURCES), "parameter_grid_budget": 0,
        "derived_after_previous_project_results": True,
        "published_research_validates_this_exact_rule": False,
        "mechanism": "Test persistence of own 20-day returns, standardized by past volatility. Daily opportunities and a longer target seek lower cost drag; neither assures positive expectancy.",
        "autonomous_ai_generation": False, "execution_authorized": False}


class MomentumReader:
    def __init__(self, bars, *, cap):
        if (type(cap) is not int or not RULES["minimum_history"] <= cap <= len(bars)
                or bars[0].timeframe != "4h" or bars[0].symbol not in SYMBOLS):
            raise ValueError("Only registered symbols, 4h and a bounded prefix are supported")
        prefix = bars[:cap]
        _check_bars(prefix, prefix[-1].closed_at + timedelta(microseconds=1), replay=True)
        self.bars, self.cap, self.cache = prefix, cap, {}

    def momentum(self, count):
        if type(count) is not int or not RULES["minimum_history"] <= count <= self.cap:
            raise ValueError("Momentum requested outside closed prefix")
        return log(self.bars[count - 1].close / self.bars[count - 1 - RULES["lookback_bars"]].close)

    def decision(self, policy_id, count, clock):
        if (policy_id != POLICY_ID or type(count) is not int
                or not RULES["minimum_history"] <= count <= self.cap
                or clock.tzinfo is None or clock.utcoffset() is None
                or self.bars[count - 1].closed_at > clock
                or (clock - self.bars[count - 1].closed_at).total_seconds() > 28800):
            raise ValueError("Unsupported policy or decision clock/prefix")
        if count in self.cache:
            return replace(self.cache[count], available_at=clock)
        bar = self.bars[count - 1]
        atr = _atr(self.bars[count - 15:count])
        d = PlaybookDecision(POLICY_ID, POLICY.version, bar.symbol, bar.timeframe, clock,
            "WAIT", bar.close, atr, None, None, None, None, None, "NOT_DAILY_OPPORTUNITY")
        stamp = bar.closed_at.astimezone(timezone.utc)
        if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) != (0, 0, 0, 0):
            self.cache[count] = d
            return d
        prices = [b.close for b in self.bars[count - 121:count]]
        sigma = pstdev(log(b / a) for a, b in zip(prices, prices[1:]))
        trend = self.momentum(count)
        if sigma <= 1e-12 or abs(trend) < sigma * sqrt(120):
            d = replace(d, reason="TREND_WEAK_OR_VOLATILITY_DEGENERATE")
        elif atr <= 0 or planning_cost_fraction() > .20 * (6 * atr / bar.close):
            d = replace(d, reason="PLANNED_COST_TOO_LARGE_FOR_TARGET")
        else:
            side = 1 if trend > 0 else -1
            stop, target = bar.close - side * 2 * atr, bar.close + side * 6 * atr
            if min(stop, target) <= 0:
                d = replace(d, reason="NONPOSITIVE_STRUCTURAL_PRICE")
            else:
                d = replace(d, action="LONG" if side == 1 else "SHORT", stop_price=stop,
                    target_price=target, invalidation_level=prices[0], setup_index=count - 1,
                    setup_id=digest([POLICY_ID, bar.symbol, bar.closed_at.isoformat()]),
                    reason="DAILY_STANDARDIZED_20D_TREND_WITH_COST_BUDGET")
        self.cache[count] = d
        return d

"""Versioned research-only parent parity and three bounded revision hypotheses.

EMA prefixes are computed incrementally with the original S2 recurrence. All
parent decisions are tested against the unchanged slow S2 evaluator. No global
patching, agent registration, risk changes or OMS integration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta, timezone
from statistics import fmean, pstdev

from app.strategy.gate_classical_s2 import ResearchPlan, STRATEGIES, TIMEFRAME_SECONDS, _atr, _check_bars, _rsi
from app.strategy.gate_s4_lab import Hypothesis, digest


@dataclass(frozen=True)
class Revision:
    parent_strategy: str
    rule: str
    rationale: str
    version: str = "1.0.0"

    def __post_init__(self):
        if (self.rule != "TARGET_OVER_MODEL_COST_AT_LEAST_3" or self.parent_strategy not in STRATEGIES
                or self.version != "1.0.0"
                or not isinstance(self.rationale, str) or not 20 <= len(self.rationale) <= 2000):
            raise ValueError("Unsupported bounded revision")

    @property
    def candidate_id(self):
        return digest({k: v for k, v in asdict(self).items() if k != "rationale"})


def revision_specs():
    return tuple(Revision(strategy, "TARGET_OVER_MODEL_COST_AT_LEAST_3",
        "Training shows cost drag can reverse positive gross results; test abstention when the target cannot cover three fixed conservative model cost budgets. This cannot repair a negative gross edge by itself.")
        for strategy in sorted(STRATEGIES))


def _ema_prefixes(values, period):
    result = [None] * min(period - 1, len(values))
    if len(values) >= period:
        value = fmean(values[:period])
        result.append(value)
        multiplier = 2 / (period + 1)
        for price in values[period:]:
            value += multiplier * (price - value)
            result.append(value)
    return result


class CausalParentPlans:
    def __init__(self, bars, *, cap):
        if not 220 <= cap <= len(bars):
            raise ValueError("Invalid causal parent cap")
        self.bars, self.cap, self.cache = bars, cap, {}
        prefix = bars[:cap]
        _check_bars(prefix, prefix[-1].closed_at + timedelta(microseconds=1), replay=True)
        self.closes = tuple(b.close for b in prefix)
        self.fast = _ema_prefixes(self.closes, 20)
        self.slow = _ema_prefixes(self.closes, 50)
        self.long = _ema_prefixes(self.closes, 200)

    def features(self, count):
        if not 80 <= count <= self.cap:
            raise ValueError("Features outside registered causal prefix")
        i = count - 1
        visible = self.bars[max(0, i - 14):count]
        atr = _atr(visible)
        closes = self.closes[max(0, i - 20):count]
        distance = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
        efficiency = abs(closes[-1] - closes[0]) / distance if distance else 0.
        volume = fmean(b.volume for b in self.bars[count - 21:count - 1])
        long = self.long[i]
        slope = long - self.long[i - 8] if long is not None and i >= 207 else None
        return {"atr": atr, "efficiency20": efficiency, "ema200": long,
                "ema200_slope8": slope, "relative_volume": self.bars[i].volume / volume if volume else 0.}

    def parent(self, strategy, count, clock):
        if (clock.tzinfo is None or clock.utcoffset() is None or strategy not in STRATEGIES
                or not 80 <= count <= self.cap
                or self.bars[count - 1].closed_at > clock
                or (clock - self.bars[count - 1].closed_at).total_seconds()
                   > 2 * TIMEFRAME_SECONDS[self.bars[0].timeframe]):
            raise ValueError("Unsupported parent decision clock/prefix")
        key = strategy, count
        if key not in self.cache:
            i = count - 1
            price, previous = self.closes[i], self.closes[i - 1]
            fast, slow, prior_fast = self.fast[i], self.slow[i], self.fast[i - 1]
            f = self.features(count)
            atr = f["atr"]
            rsi = _rsi(self.closes[i - 14:i + 1])
            action, reason = "WAIT", "Context or confirmation absent"
            if atr > 0:
                if strategy == "EMA_TREND_PULLBACK_V1":
                    if fast > slow and previous <= prior_fast and price > fast and 45 <= rsi <= 75:
                        action, reason = "LONG", "Trend-up EMA20 pullback reclaim, RSI confirmed"
                    elif fast < slow and previous >= prior_fast and price < fast and 25 <= rsi <= 55:
                        action, reason = "SHORT", "Trend-down EMA20 pullback rejection, RSI confirmed"
                elif strategy == "DONCHIAN_VOLUME_BREAKOUT_V1":
                    upper = max(b.high for b in self.bars[count - 21:count - 1])
                    lower = min(b.low for b in self.bars[count - 21:count - 1])
                    if f["relative_volume"] >= 1.2 and price > upper and fast > slow:
                        action, reason = "LONG", "20-bar high broken on above-baseline volume"
                    elif f["relative_volume"] >= 1.2 and price < lower and fast < slow:
                        action, reason = "SHORT", "20-bar low broken on above-baseline volume"
                else:
                    last20 = self.closes[count - 20:count]
                    mid, sigma = fmean(last20), pstdev(last20)
                    if abs(fast - slow) / price < .004 and sigma > 0:
                        if price < mid - 1.7 * sigma and rsi <= 40:
                            action, reason = "LONG", "Ranging market, below lower Bollinger band and RSI weak"
                        elif price > mid + 1.7 * sigma and rsi >= 60:
                            action, reason = "SHORT", "Ranging market, above upper Bollinger band and RSI extended"
            spec = STRATEGIES[strategy]
            stop = spec.stop_atr * atr if action != "WAIT" else None
            self.cache[key] = ResearchPlan(strategy, spec.version, self.bars[0].symbol,
                self.bars[0].timeframe, clock.astimezone(timezone.utc), action, stop,
                spec.reward_to_risk * stop if stop is not None else None, reason, False, False)
        return replace(self.cache[key], cutoff=clock.astimezone(timezone.utc))

    def plan(self, strategy, prefix, clock, *, hypothesis=None, delayed=False):
        if not prefix or len(prefix) > self.cap or prefix[-1] is not self.bars[len(prefix) - 1]:
            raise ValueError("Expected original closed causal prefix")
        count = len(prefix) - int(delayed)
        plan = self.parent(strategy, count, clock)
        if hypothesis is None or plan.action == "WAIT":
            return plan
        if hypothesis.parent_strategy != strategy:
            raise ValueError("Hypothesis parent mismatch")
        f, price = self.features(count), self.closes[count - 1]
        if isinstance(hypothesis, Hypothesis):
            accepted = (f["relative_volume"] >= hypothesis.threshold
                        if hypothesis.filter_name == "RELATIVE_VOLUME_MIN" else
                        f["atr"] / price <= hypothesis.threshold)
        else:
            # Uses a fixed doubled roundtrip cost budget + one funding interval.
            # It does not change thresholds when the replay scenario changes.
            accepted = plan.target_distance >= 3 * price * (2 * (12 + 12) + 2) / 10000
        if not accepted:
            return replace(plan, action="WAIT", stop_distance=None, target_distance=None,
                           reasoning="Registered S4 revision abstention")
        return plan

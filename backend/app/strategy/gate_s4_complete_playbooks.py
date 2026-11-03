"""Two complete, fixed research hypotheses. No exchange/OMS/agent registration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from app.strategy.gate_s4_lab import digest
from app.strategy.gate_s4_revision_engine import CausalParentPlans


@dataclass(frozen=True)
class Playbook:
    policy_id: str
    version: str
    mechanism: str
    max_hold_bars: int
    cooldown_bars: int
    setup_expiry_bars: int
    reward_to_risk: float
    parent_control: str


PLAYBOOKS = {
    "BREAKOUT_RETEST_V1": Playbook("BREAKOUT_RETEST_V1", "1.0.0", "TREND_RETEST", 20, 4, 8, 2.,
                                    "DONCHIAN_VOLUME_BREAKOUT_V1"),
    "FAILED_BREAKOUT_V1": Playbook("FAILED_BREAKOUT_V1", "1.0.0", "RANGE_FAILURE", 12, 4, 5, 1.25,
                                   "RANGE_BOLLINGER_REVERSION_V1"),
}

# All constants are part of the registered source hash; no parameter search.
RULES = {"channel_bars": 20, "break_buffer_atr": .1, "retest_band_atr": .25,
         "stop_buffer_atr": .25, "minimum_stop_atr": .5, "maximum_stop_atr": 3.,
         "maximum_entry_drift_atr": .5, "range_min_width_atr": 2.,
         "range_max_width_atr": 8., "range_max_ema_spread_atr": .5,
         "breakout_volume_ratio": 1.2, "base_entry_lag_bars": 2,
         "invalidation_exit_lag_bars": 2}


@dataclass(frozen=True)
class PlaybookDecision:
    policy_id: str
    version: str
    symbol: str
    timeframe: str
    available_at: datetime
    action: str
    reference_price: float
    atr: float
    stop_price: float | None
    target_price: float | None
    invalidation_level: float | None
    setup_index: int | None
    setup_id: str | None
    reason: str
    execution_authorized: bool = False
    predictive_edge_proven: bool = False


class PlaybookReader:
    def __init__(self, bars, *, cap):
        self.bars, self.cap = bars, cap
        self.context = CausalParentPlans(bars, cap=cap)
        self.cache = {}

    def decision(self, policy_id, count, clock):
        if (policy_id not in PLAYBOOKS or not 220 <= count <= self.cap
                or clock.tzinfo is None or self.bars[count - 1].closed_at > clock):
            raise ValueError("Unregistered playbook or future/invalid prefix")
        key = policy_id, count
        if key in self.cache:
            from dataclasses import replace
            return replace(self.cache[key], available_at=clock)
        policy = PLAYBOOKS[policy_id]
        i, current = count - 1, self.bars[count - 1]
        atr = self.context.features(count)["atr"]
        base = dict(policy_id=policy_id, version=policy.version, symbol=current.symbol,
                    timeframe=current.timeframe, available_at=clock, reference_price=current.close,
                    atr=atr, execution_authorized=False, predictive_edge_proven=False)
        answer = PlaybookDecision(**base, action="WAIT", stop_price=None, target_price=None,
                                  invalidation_level=None, setup_index=None, setup_id=None,
                                  reason="NO_CONFIRMED_SETUP")
        if atr <= 0:
            return answer
        # Search closed historical triggers only; the most recent valid one wins.
        offsets = range(2, 7) if policy.mechanism == "TREND_RETEST" else range(1, 3)
        for offset in offsets:
            k = i - offset
            prior = self.bars[k - 20:k]
            trigger = self.bars[k]
            trigger_atr = self.context.features(k + 1)["atr"]
            upper, lower = max(b.high for b in prior), min(b.low for b in prior)
            mid = (upper + lower) / 2
            volume = sum(b.volume for b in prior) / 20
            fast, slow = self.context.fast[k], self.context.slow[k]
            side = None
            stop = target = level = None
            if policy.mechanism == "TREND_RETEST":
                if volume <= 0 or trigger.volume < 1.2 * volume:
                    continue
                after = self.bars[k + 1:i]
                if (trigger.close > upper + .1 * trigger_atr and fast > slow
                        and any(b.low <= upper + .25 * trigger_atr for b in after)
                        and all(b.close >= upper - .25 * trigger_atr for b in self.bars[k + 1:count])
                        and current.close > upper + .1 * atr and current.close > current.open
                        and current.close > self.bars[i - 1].close):
                    side, level = "LONG", upper
                    stop = min(min(b.low for b in self.bars[k + 1:count]) - .25 * atr,
                               current.close - .5 * atr)
                    target = current.close + 2 * (current.close - stop)
                elif (trigger.close < lower - .1 * trigger_atr and fast < slow
                        and any(b.high >= lower - .25 * trigger_atr for b in after)
                        and all(b.close <= lower + .25 * trigger_atr for b in self.bars[k + 1:count])
                        and current.close < lower - .1 * atr and current.close < current.open
                        and current.close < self.bars[i - 1].close):
                    side, level = "SHORT", lower
                    stop = max(max(b.high for b in self.bars[k + 1:count]) + .25 * atr,
                               current.close + .5 * atr)
                    target = current.close - 2 * (stop - current.close)
            else:
                before_atr = self.context.features(k)["atr"]
                if (before_atr <= 0 or not 2 <= (upper - lower) / before_atr <= 8
                        or abs(self.context.fast[k - 1] - self.context.slow[k - 1]) > .5 * before_atr):
                    continue
                if (trigger.close > upper + .1 * trigger_atr and current.close < upper - .1 * atr
                        and current.close < current.open and current.close < self.bars[i - 1].close):
                    side, level, target = "SHORT", upper, mid
                    stop = max(b.high for b in self.bars[k:count]) + .25 * atr
                elif (trigger.close < lower - .1 * trigger_atr and current.close > lower + .1 * atr
                        and current.close > current.open and current.close > self.bars[i - 1].close):
                    side, level, target = "LONG", lower, mid
                    stop = min(b.low for b in self.bars[k:count]) - .25 * atr
            if side is None:
                continue
            direction = 1 if side == "LONG" else -1
            risk, reward = direction * (current.close - stop), direction * (target - current.close)
            if (min(stop, target) <= 0 or not .5 * atr <= risk <= 3 * atr
                    or reward / risk < policy.reward_to_risk - 1e-10):
                continue
            identity = digest([policy_id, current.symbol, current.timeframe,
                               trigger.closed_at.isoformat(), side, level])
            answer = PlaybookDecision(**base, action=side, stop_price=stop, target_price=target,
                invalidation_level=level, setup_index=k, setup_id=identity,
                reason="CLOSED_RETEST_CONFIRMATION" if policy.mechanism == "TREND_RETEST"
                       else "CLOSED_RETURN_INSIDE_PRIOR_RANGE")
            break
        self.cache[key] = answer
        return answer


def playbook_manifest():
    return {"playbooks": {k: asdict(v) for k, v in PLAYBOOKS.items()}, "rules": dict(RULES),
            "parameter_search_performed": False, "autonomous_ai_generation": False}

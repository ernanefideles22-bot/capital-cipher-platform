"""Governed research-only strategy hypotheses, independent of OMS/PAPER execution.

Agents may combine approved signals/parameters to invent new hypotheses. They
cannot submit executable Python, write to broker adapters, or promote themselves.
Evidence must come from a separate evaluator; positive backtests authorize
human review ONLY, never paper/testnet/live execution automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

ALLOWED_FEATURES = frozenset({
    "trend_up", "trend_down", "regime_range", "regime_high_volatility",
    "pullback_ema20", "breakout_up", "breakout_down", "volume_confirmation",
    "structure_higher_lows", "structure_lower_highs", "support_rejection",
    "resistance_rejection", "momentum_positive", "momentum_negative",
    "rsi_oversold", "rsi_overbought", "h4_trend_alignment",
})
ALLOWED_FAMILIES = frozenset({
    "TREND_PULLBACK", "BREAKOUT_VOLUME", "MOMENTUM_REGIME",
    "RANGE_MEAN_REVERSION", "STRUCTURE_CONFIRMATION", "CUSTOM_COMBINATION",
})
PARAMETER_LIMITS = {
    "atr_stop_multiple": (0.5, 5.0),
    "target_r_multiple": (1.0, 6.0),
    "volume_ratio_min": (0.5, 4.0),
    "ema_fast": (5.0, 50.0),
    "ema_slow": (20.0, 200.0),
    "cooldown_bars": (0.0, 48.0),
}


@dataclass(frozen=True)
class StrategyHypothesis:
    author_agent: str
    name: str
    family: str
    symbol: str
    timeframe: str
    side: str
    entry_features: tuple[str, ...]
    invalidation_feature: str
    parameters: tuple[tuple[str, float], ...]
    thesis: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{2,127}", self.author_agent):
            raise ValueError("Invalid agent identity")
        if not (5 <= len(self.name) <= 128 and 15 <= len(self.thesis) <= 2000):
            raise ValueError("A descriptive name and falsifiable thesis are required")
        if self.family not in ALLOWED_FAMILIES:
            raise ValueError("Unrecognized research family")
        if self.timeframe not in {"15m", "1h", "4h"}:
            raise ValueError("Unsupported research timeframe")
        if not re.fullmatch(r"[A-Z0-9._-]{2,32}", self.symbol):
            raise ValueError("Invalid research symbol")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("Only explicit directional research is supported")
        if not 1 <= len(self.entry_features) <= 8:
            raise ValueError("Provide 1-8 deterministic entry predicates")
        if len(set(self.entry_features)) != len(self.entry_features):
            raise ValueError("Duplicate entry predicates")
        if not set(self.entry_features) <= ALLOWED_FEATURES:
            raise ValueError("Executable code or unapproved features forbidden")
        if self.invalidation_feature not in ALLOWED_FEATURES:
            raise ValueError("Unapproved invalidation predicate")
        if "trend_up" in self.entry_features and "trend_down" in self.entry_features:
            raise ValueError("Mutually incompatible trend filters")
        if "breakout_up" in self.entry_features and "breakout_down" in self.entry_features:
            raise ValueError("Mutually incompatible breakouts")
        keys = [key for key, _ in self.parameters]
        if len(keys) != len(set(keys)) or set(keys) - PARAMETER_LIMITS.keys():
            raise ValueError("Duplicate or unapproved parameter")
        for key, value in self.parameters:
            low, high = PARAMETER_LIMITS[key]
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Unsafe parameter: {key}")
        values = dict(self.parameters)
        if values.get("ema_fast", 20) >= values.get("ema_slow", 50):
            raise ValueError("EMA fast must be below EMA slow")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def decision_authority(self) -> bool:
        return False


@dataclass(frozen=True)
class OutOfSampleEvidence:
    """Snapshot signed/verified by a SEPARATE evaluation process, not the agent."""

    hypothesis_fingerprint: str
    training_ends: datetime
    test_starts: datetime
    test_ends: datetime
    out_of_sample_trades: int
    walk_forward_folds: int
    net_return_percent: float
    benchmark_net_return_percent: float
    worst_drawdown_percent: float
    multiple_testing_adjusted_p_value: float
    stressed_cost_bps_per_side: float
    lookahead_audit_passed: bool
    venue_netting_audit_passed: bool
    verified_by_independent_runner: bool
    evidence_sha256: str


@dataclass(frozen=True)
class ResearchReview:
    status: str
    reasons: tuple[str, ...]
    execution_authorized: bool = False


def review_hypothesis(
    hypothesis: StrategyHypothesis,
    evidence: OutOfSampleEvidence | None,
) -> ResearchReview:
    """A pre-review eligibility check, NEVER a trading approval.

    Strict initial defaults, to be revisited after a real cost/venue study.
    These numerical thresholds are research policy, not market facts.
    """
    if evidence is None:
        return ResearchReview("RESEARCH_ONLY", ("No independent OOS evidence",))
    failures: list[str] = []
    if evidence.hypothesis_fingerprint != hypothesis.fingerprint:
        failures.append("Evidence/hypothesis fingerprint mismatch")
    for timestamp in (
        evidence.training_ends, evidence.test_starts, evidence.test_ends
    ):
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            failures.append("Naive timestamp is invalid")
            break
    if (
        evidence.training_ends.tzinfo is not None
        and evidence.test_starts.tzinfo is not None
        and evidence.test_ends.tzinfo is not None
        and not (evidence.training_ends < evidence.test_starts < evidence.test_ends)
    ):
        failures.append("Temporal train/test isolation failed")
    if evidence.out_of_sample_trades < 100:
        failures.append("Insufficient out-of-sample closed trades")
    if evidence.walk_forward_folds < 3:
        failures.append("Insufficient independent test folds")
    metrics = (
        evidence.net_return_percent,
        evidence.benchmark_net_return_percent,
        evidence.worst_drawdown_percent,
        evidence.multiple_testing_adjusted_p_value,
        evidence.stressed_cost_bps_per_side,
    )
    if not all(math.isfinite(x) for x in metrics):
        failures.append("Non-finite economic statistics")
    else:
        if evidence.net_return_percent <= 0:
            failures.append("Net OOS performance is not positive")
        if evidence.net_return_percent <= evidence.benchmark_net_return_percent:
            failures.append("No positive incremental net performance")
        if not 0 <= evidence.worst_drawdown_percent <= 15:
            failures.append("Drawdown outside research tolerance")
        if not 0 <= evidence.multiple_testing_adjusted_p_value < 0.05:
            failures.append("Multiple-testing corrected significance failed")
        if evidence.stressed_cost_bps_per_side < 12:
            failures.append("Stressed execution cost below 12 bps/side")
    if not evidence.lookahead_audit_passed:
        failures.append("Lookahead/purging audit failed")
    if not evidence.venue_netting_audit_passed:
        failures.append("Venue position-netting rules not validated")
    if not evidence.verified_by_independent_runner:
        failures.append("Independent evaluation not attested")
    if not re.fullmatch("[a-f0-9]{64}", evidence.evidence_sha256):
        failures.append("Evidence integrity digest invalid")
    return ResearchReview(
        "RESEARCH_ONLY" if failures else "ELIGIBLE_FOR_HUMAN_REVIEW",
        tuple(failures),
    )

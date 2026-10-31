"""Regression tests for read-only candle context and proposed strategies."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.market_intelligence import analyze_closed_candles
from app.strategy.research_lab import (
    OutOfSampleEvidence,
    StrategyHypothesis,
    review_hypothesis,
)


def candles(count=80, *, rising=True):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = []
    for i in range(count):
        base = 100 + (i * 0.4 if rising else 0)
        close = base + 0.2
        stamp = start + timedelta(minutes=15 * (i + 1))
        items.append(Candle(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            timeframe="15m",
            open=base,
            high=close + 0.3,
            low=base - 0.3,
            close=close,
            volume=100.0,
            closed_at=stamp,
            received_at=stamp + timedelta(seconds=1),
        ))
    return items


def inspect(bars):
    return analyze_closed_candles(
        bars, as_of=bars[-1].received_at + timedelta(seconds=1)
    )


def proposal():
    return StrategyHypothesis(
        author_agent="ResearchAgent01",
        name="Momentum and volume research",
        family="CUSTOM_COMBINATION",
        symbol="BTCUSDT",
        timeframe="15m",
        side="BUY",
        entry_features=("trend_up", "pullback_ema20", "volume_confirmation"),
        invalidation_feature="trend_down",
        parameters=(("atr_stop_multiple", 1.5), ("target_r_multiple", 2.0)),
        thesis="When an uptrend pulls back to EMA20 with positive volume, test whether continuation persists.",
    )


def evidence(hypothesis):
    return OutOfSampleEvidence(
        hypothesis_fingerprint=hypothesis.fingerprint,
        training_ends=datetime(2025, 8, 1, tzinfo=timezone.utc),
        test_starts=datetime(2025, 8, 3, tzinfo=timezone.utc),
        test_ends=datetime(2026, 1, 1, tzinfo=timezone.utc),
        out_of_sample_trades=150,
        walk_forward_folds=4,
        net_return_percent=9,
        benchmark_net_return_percent=2,
        worst_drawdown_percent=9,
        multiple_testing_adjusted_p_value=0.01,
        stressed_cost_bps_per_side=12,
        lookahead_audit_passed=True,
        venue_netting_audit_passed=True,
        verified_by_independent_runner=True,
        evidence_sha256="a" * 64,
    )


def test_market_context_from_closed_candles_only():
    x = inspect(candles())
    assert x.symbol == "BTCUSDT"
    assert x.regime == "UPTREND"
    assert x.ema20 > x.ema50
    assert x.atr14 > 0
    assert x.relative_volume == 1.0
    assert not hasattr(x, "place_order")


def test_market_context_rejects_unavailable_future_bar():
    bars = candles()
    cutoff = bars[-2].received_at
    with pytest.raises(ValueError, match="Future/unavailable"):
        analyze_closed_candles(bars, as_of=cutoff)


def test_market_context_rejects_duplicates_and_mixed_timeframes():
    bars = candles()
    with pytest.raises(ValueError, match="strictly increasing"):
        inspect([*bars[:-1], bars[-2]])
    mixed = [*bars[:-1], bars[-1].model_copy(update={"timeframe": "1h"})]
    with pytest.raises(ValueError, match="Mixed"):
        inspect(mixed)


def test_market_context_requires_60_bars_and_no_volume_division():
    with pytest.raises(ValueError, match="60"):
        inspect(candles(59))
    bars = [
        x.model_copy(update={"volume": 0})
        for x in candles()
    ]
    assert inspect(bars).relative_volume == 0.0


def test_agent_hypothesis_is_immutable_fingerprinted_and_orderless():
    p = proposal()
    assert len(p.fingerprint) == 64
    assert not p.decision_authority
    with pytest.raises(Exception):
        p.side = "SELL"
    assert replace(p, side="SELL").fingerprint != p.fingerprint


@pytest.mark.parametrize("change", [
    {"timeframe": "5m"},
    {"entry_features": ("trend_up", "trade_now()")},
    {"entry_features": ("trend_up", "trend_down")},
    {"parameters": (("atr_stop_multiple", 100.0),)},
    {"parameters": (("atr_stop_multiple", float("nan")),)},
])
def test_unapproved_or_unsafe_hypotheses_rejected(change):
    with pytest.raises(ValueError):
        replace(proposal(), **change)


def test_economic_pre_review_is_not_execution_permission():
    p = proposal()
    r = review_hypothesis(p, evidence(p))
    assert r.status == "ELIGIBLE_FOR_HUMAN_REVIEW"
    assert r.execution_authorized is False


@pytest.mark.parametrize("change", [
    {"hypothesis_fingerprint": "0" * 64},
    {"out_of_sample_trades": 12},
    {"walk_forward_folds": 1},
    {"net_return_percent": -1},
    {"benchmark_net_return_percent": 10},
    {"worst_drawdown_percent": 30},
    {"multiple_testing_adjusted_p_value": 0.4},
    {"stressed_cost_bps_per_side": 0},
    {"lookahead_audit_passed": False},
    {"venue_netting_audit_passed": False},
    {"verified_by_independent_runner": False},
    {"evidence_sha256": "bad"},
    {"test_starts": datetime(2025, 7, 1, tzinfo=timezone.utc)},
])
def test_invalid_or_unprofitable_evidence_never_eligible(change):
    p = proposal()
    r = review_hypothesis(p, replace(evidence(p), **change))
    assert r.status == "RESEARCH_ONLY"
    assert r.reasons
    assert r.execution_authorized is False


def test_missing_evidence_never_authorizes_execution():
    assert review_hypothesis(proposal(), None).status == "RESEARCH_ONLY"

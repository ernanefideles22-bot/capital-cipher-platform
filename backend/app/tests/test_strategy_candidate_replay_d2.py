"""D2 regression tests: own predicate replay, temporal fills and one-way gate."""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.backtest import WalkForwardProtocol, WalkForwardResearchPlan
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.candidate_replay import (
    OneWayExposureGuard, replay_candidate,
)
from app.strategy.candidate_walk_forward import evaluate_candidate_walk_forward
from app.strategy.research_lab import StrategyHypothesis
from app.strategy.research_memory import ResearchMemory


def sample_bars(n=120):
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(n):
        price = 100.0 + i * 0.25
        closing = t0 + timedelta(minutes=15 * (i + 1))
        candles.append(Candle(
            exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe="15m",
            open=price, high=price + .25, low=price - .15,
            close=price + .1, volume=100,
            closed_at=closing, received_at=closing + timedelta(seconds=1),
        ))
    return candles


def proposal(*, features=("momentum_positive",),
             invalidation="momentum_negative"):
    return StrategyHypothesis(
        author_agent="CandidateResearchAgent",
        name="Momentum from received candles",
        family="CUSTOM_COMBINATION",
        symbol="BTCUSDT", timeframe="15m", side="BUY",
        entry_features=features,
        invalidation_feature=invalidation,
        parameters=(("atr_stop_multiple", 1.5),
                    ("target_r_multiple", 2.0)),
        thesis="Evaluate whether persistence of momentum after all modeled costs beats a naive baseline.",
    )


def test_agent_own_predicate_generates_trades_with_delayed_open():
    bars = sample_bars()
    report = replay_candidate(proposal(), bars)
    assert report.trades
    assert report.trades[0].entry_at == (
        bars[62].closed_at - timedelta(minutes=15)
    )
    assert report.trades[0].execution_costs > 0
    assert report.total_costs > 0
    assert report.execution_authorized is False
    assert report.status == "RESEARCH_ONLY"
    assert report.venue_policy == "SINGLE_OWNER_ONE_WAY"


def test_when_both_brackets_touch_simulator_chooses_stop():
    bars = sample_bars()
    entry = bars[62]
    bars[62] = entry.model_copy(update={
        "high": entry.open + 20,
        "low": entry.open - 20,
    })
    report = replay_candidate(proposal(), bars)
    assert report.trades[0].exit_reason == "STOP_LOSS"
    assert report.trades[0].net_pnl < 0


def test_rule_invalidation_latches_first_receipt_and_exits_next_eligible_open():
    bars = sample_bars()
    report = replay_candidate(
        proposal(invalidation="momentum_positive"), bars
    )
    assert report.trades[0].exit_reason == "RULE_INVALIDATED"
    assert report.trades[0].exit_at == (
        bars[64].closed_at - timedelta(minutes=15)
    )


def test_stop_gap_outranks_pending_invalidation():
    bars = sample_bars()
    original = bars[64]
    price = original.open - 5
    bars[64] = original.model_copy(update={
        "open": price, "high": price + 0.25,
        "low": price - 0.15, "close": price + 0.1,
    })
    report = replay_candidate(
        proposal(invalidation="momentum_positive"), bars
    )
    assert report.trades[0].exit_reason == "STOP_LOSS"
    assert report.trades[0].exit_price == price


def test_unavailable_candle_cannot_be_used_as_signal():
    bars = sample_bars()
    bars[60] = bars[60].model_copy(update={
        "received_at": bars[60].closed_at - timedelta(seconds=1),
    })
    with pytest.raises(ValueError, match="before it closed"):
        replay_candidate(proposal(), bars)


def test_unsupported_cross_timeframe_predicate_fails_closed():
    with pytest.raises(ValueError, match="Unsupported research"):
        replay_candidate(
            proposal(features=("h4_trend_alignment",)), sample_bars()
        )


def test_duplicate_or_missing_candles_fail_closed():
    bars = sample_bars()
    with pytest.raises(ValueError, match="irregular"):
        replay_candidate(proposal(), bars[:60] + bars[61:])
    with pytest.raises(ValueError, match="irregular"):
        replay_candidate(proposal(), bars[:60] + [bars[59]] + bars[60:])


def test_execution_cost_cannot_be_optimized_below_floor():
    with pytest.raises(ValueError, match="12"):
        replay_candidate(proposal(), sample_bars(),
                         all_in_cost_bps_per_side=0.0)


def test_mismatched_timeframe_fails_closed():
    bars = sample_bars()
    bars[4] = bars[4].model_copy(update={"timeframe": "1h"})
    with pytest.raises(ValueError, match="universe"):
        replay_candidate(proposal(), bars)


def test_one_way_guard_refuses_opposing_and_duplicate_books():
    guard = OneWayExposureGuard()
    guard.reserve("ETHUSDT", "BUY", "SCALP")
    with pytest.raises(ValueError, match="concurrent"):
        guard.reserve("ETHUSDT", "SELL", "DAY")
    with pytest.raises(ValueError, match="concurrent"):
        guard.reserve("ETHUSDT", "BUY", "SWING")
    with pytest.raises(ValueError, match="identity mismatch"):
        guard.release("ETHUSDT", "DAY")
    guard.release("ETHUSDT", "SCALP")
    guard.reserve("ETHUSDT", "SELL", "DAY")
    assert guard.is_reserved("ETHUSDT")


def test_replay_is_deterministic_and_not_rewriting_hypothesis():
    bars = sample_bars()
    p = proposal()
    a = replay_candidate(p, bars)
    b = replay_candidate(p, bars)
    assert a == b
    assert p.fingerprint == a.fingerprint


def test_candidate_walk_forward_evaluates_hypothesis_and_keeps_it_in_research(tmp_path):
    bars = sample_bars(700)
    memory = ResearchMemory(tmp_path / "candidate.db")
    p = proposal()
    protocol = WalkForwardProtocol(
        train_candles=250, validation_candles=100, test_candles=100,
        embargo_candles=1, max_folds=3,
    )
    first = evaluate_candidate_walk_forward(
        p, bars, journal=memory, protocol=protocol,
        research_plan=WalkForwardResearchPlan(candidate_budget=10),
    )
    second = evaluate_candidate_walk_forward(
        p, bars, journal=memory, protocol=protocol,
        research_plan=WalkForwardResearchPlan(candidate_budget=10),
    )
    assert first == second
    assert len(first.folds) == 3
    assert first.status == "RESEARCH_ONLY"
    assert not first.execution_authorized
    assert all(f.test_trades > 0 for f in first.folds)
    assert first.trial_budget == 10
    assert len(memory.list_events()) == 2
    assert memory.verify_chain() == memory.list_events()[-1].event_hash


def test_walk_forward_requires_three_folds_and_embargo(tmp_path):
    memory = ResearchMemory(tmp_path / "candidate.db")
    bars = sample_bars(700)
    p = proposal()
    with pytest.raises(ValueError, match="Positive embargo"):
        evaluate_candidate_walk_forward(
            p, bars, journal=memory,
            protocol=WalkForwardProtocol(embargo_candles=0),
        )
    with pytest.raises(ValueError, match="three"):
        evaluate_candidate_walk_forward(
            p, bars, journal=memory,
            protocol=WalkForwardProtocol(max_folds=2),
        )
    assert memory.list_events() == []

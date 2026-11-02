"""Entry/exit causality, economic identities, frozen controls and future guards."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import gzip
import json
import shutil

import pytest

from app.schemas.market import Candle
from app.strategy import gate_cost_viability_research as cost
from app.strategy.gate_classical_s2 import ResearchPlan
from app.strategy.gate_entry_exit_research import (
    EntryExitBook, adjust_proposal, historical_case, holdout_case, HYPOTHESES,
)
from scripts import gate_entry_exit_experiment as runner
from scripts.gate_s3_economic_diagnostic import read_snapshot
from app.tests.test_gate_s2c_confluence import make_bars

STRATEGY = "EMA_TREND_PULLBACK_V1"


def proposal(side="LONG"):
    rows = make_bars("15m", descending=(side == "SHORT"))
    old, new = rows[-2:]
    return ResearchPlan(STRATEGY, "1.0.0", "BTCUSDT", "15m",
                        old.closed_at+timedelta(microseconds=1), side, 1, 2,
                        "SYNTHETIC_TEST", False, False), old, new


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_confirmation_uses_later_closed_bar_and_preserves_the_saved_stop(side):
    p, old, new = proposal(side)
    cutoff = new.closed_at+timedelta(microseconds=1)
    confirmed = adjust_proposal(p, old, new, cutoff, "CONFIRM_CLOSE")
    assert confirmed.action == side
    assert confirmed.stop_distance == p.stop_distance
    assert confirmed.target_distance == p.target_distance
    assert confirmed.cutoff == cutoff > p.cutoff
    assert confirmed.execution_authorized is False
    assert p.cutoff == old.closed_at+timedelta(microseconds=1)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_touching_saved_stop_invalidates_confirmation_even_if_close_recovers(side):
    p, old, new = proposal(side)
    updates = {"low": old.close-1} if side == "LONG" else {"high": old.close+1}
    touched = new.model_copy(update=updates)
    out = adjust_proposal(p, old, touched, new.closed_at+timedelta(microseconds=1), "CONFIRM_CLOSE")
    assert out.action == "WAIT" and out.stop_distance is None and out.target_distance is None


def test_equal_close_is_not_confirmation_and_two_bar_delay_is_forbidden():
    p, old, new = proposal()
    flat = new.model_copy(update={"close": old.close})
    assert adjust_proposal(p, old, flat, new.closed_at+timedelta(microseconds=1), "CONFIRM_CLOSE").action == "WAIT"
    later = new.model_copy(update={"closed_at": new.closed_at+timedelta(minutes=15)})
    with pytest.raises(ValueError, match="previous"):
        adjust_proposal(p, old, later, later.closed_at+timedelta(microseconds=1), "CONFIRM_CLOSE")


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_net_target_equation_has_two_to_one_estimated_reward_after_costs(side):
    p, old, new = proposal(side)
    out = adjust_proposal(p, old, new, new.closed_at+timedelta(microseconds=1), "CONFIRM_CLOSE_NET_RR2")
    c = new.close*.0025  # 2*(6+6) bps plus one max-hold funding interval
    assert (out.target_distance-c)/(out.stop_distance+c) == pytest.approx(2)
    assert out.stop_distance == p.stop_distance
    assert out.target_distance >= p.target_distance
    high_target = replace(p, target_distance=100)
    assert adjust_proposal(high_target, old, new, new.closed_at+timedelta(microseconds=1),
                           "CONFIRM_CLOSE_NET_RR2").target_distance == 100


def test_target_only_uses_current_signal_without_a_confirmation_delay():
    p, old, new = proposal()
    now = new.closed_at+timedelta(microseconds=1)
    current = replace(p, cutoff=now)
    result = adjust_proposal(current, new, new, now, "NET_RR2")
    assert result.action == "LONG" and result.cutoff == current.cutoff
    with pytest.raises(ValueError, match="current proposal"):
        adjust_proposal(p, old, new, now, "NET_RR2")


@pytest.mark.parametrize("changes", [{"execution_authorized": True},
    {"predictive_edge_proven": True}, {"strategy_version": "2"}, {"stop_distance": -1}])
def test_executable_or_corrupt_plans_fail_closed(changes):
    p, old, new = proposal()
    with pytest.raises(ValueError):
        adjust_proposal(replace(p, **changes), old, new,
                        new.closed_at+timedelta(microseconds=1), "CONFIRM_CLOSE_NET_RR2")


def load_groups(symbol="BTCUSDT"):
    snap, _ = read_snapshot(runner.SNAPSHOT)
    return runner.groups_from(snap, symbol)


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
def test_original_and_s1_accounts_are_unchanged_for_both_cost_scenarios(tf):
    groups = load_groups()
    book = EntryExitBook(groups, tf)
    old = cost.DecisionBook(groups[tf], {k: groups[k] for k in ("1h", "4h")} if tf == "15m" else None)
    for strategy in cost.STRATEGIES:
        for variant in ("BASELINE",)+(("S1",) if tf == "15m" else ()):
            for friction in (cost.BASE_FRICTION, cost.STRESS_FRICTION):
                a = book.run(strategy, variant, 480, 800, friction)
                b = old.run(strategy, variant, 480, 800, friction=friction)
                assert a.trades == b.trades and a.net_pnl == b.net_pnl


def test_real_training_choice_is_invariant_to_valid_later_candle_changes():
    groups = load_groups("SOLUSDT")
    strategy = "RANGE_BOLLINGER_REVERSION_V1"
    first = historical_case(groups, strategy, "4h")["chronological_folds"][0]
    altered = dict(groups)
    altered["4h"] = tuple(b.model_copy(update={"high": b.high+1000}) if i >= 400 else b
                           for i, b in enumerate(groups["4h"]))
    other = historical_case(altered, strategy, "4h")["chronological_folds"][0]
    assert first["selected"] == other["selected"] == "CONFIRM_CLOSE_NET_RR2"
    assert first["training"] == other["training"]


def synthetic_groups(start, *, after_hours=20):
    output = {}
    for tf in ("15m", "1h", "4h"):
        rows = make_bars(tf, n=200)
        shift = start+timedelta(hours=after_hours)-rows[-1].closed_at
        output[tf] = tuple(b.model_copy(update={"closed_at": b.closed_at+shift,
                                               "received_at": b.received_at+shift}) for b in rows)
    return output


def test_unseen_window_cannot_refit_or_rescue_an_abstaining_training_choice():
    start = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    old = {"timeframe": "15m", "strategy": STRATEGY, "holdout_selected": None}
    result = holdout_case(synthetic_groups(start), old, start)
    assert result["selected"] is None
    assert not result["selection_refit_on_holdout"]
    assert result["status"] == "INSUFFICIENT_POST_FREEZE_EVIDENCE"
    assert set(result["comparison"]["base"]) == {"BASELINE", "S1"}
    assert result["evidence"]["profitability_proven"] is False


def test_missing_beginning_of_holdout_is_not_replaced_by_a_later_window():
    start = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    old = {"timeframe": "15m", "strategy": STRATEGY, "holdout_selected": "CONFIRM_CLOSE"}
    with pytest.raises(ValueError, match="lost holdout"):
        holdout_case(synthetic_groups(start, after_hours=200), old, start)


def test_pre_freeze_origin_is_not_delayed_into_the_holdout(monkeypatch):
    start = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    groups = synthetic_groups(start)
    book = EntryExitBook(groups, "15m", earliest_origin_close=start+timedelta(minutes=15))
    def signal(strategy, prefix, *, as_of, historical_replay):
        return ResearchPlan(strategy, "1.0.0", "BTCUSDT", "15m", as_of,
                            "LONG", 1, 2, "SYNTHETIC", False, False)
    monkeypatch.setattr(cost, "evaluate_strategy", signal)
    i = next(i for i, b in enumerate(book.primary) if b.closed_at == start+timedelta(minutes=15))
    now = book.primary[i].closed_at+timedelta(microseconds=1)
    assert book.evaluate(STRATEGY, book.primary[:i+1], now, "CONFIRM_CLOSE").action == "WAIT"


def test_old_data_are_reported_as_waiting_instead_of_future_validation():
    result = runner.post_freeze(runner.SNAPSHOT)
    assert result["status"] == "WAITING_FOR_POST_FREEZE_DATA"
    assert result["profitability_proven"] is False


def test_holdout_rejects_source_edits_or_training_file_replacement(monkeypatch, tmp_path):
    with monkeypatch.context() as m:
        m.setattr(runner, "source_hashes", lambda: {"changed": "hash"})
        with pytest.raises(ValueError, match="source changed"):
            runner.frozen_training()
    for name in ("protocol.json", "freeze.json", "results.json.gz"):
        shutil.copyfile(runner.DIR/name, tmp_path/name)
    path = tmp_path/"results.json.gz"
    path.write_bytes(path.read_bytes()+b"changed")
    monkeypatch.setattr(runner, "DIR", tmp_path)
    with pytest.raises(ValueError, match="training result changed"):
        runner.frozen_training()

"""Independent economic oracles, causal selection and frozen replay regression."""
from dataclasses import asdict, replace
from datetime import timedelta
import gzip
import json

import pytest

from app.strategy import gate_cost_viability_research as cost
from app.strategy.gate_classical_s2 import ResearchPlan
from app.strategy.gate_s2_replay import Friction, replay_segment
from app.tests.test_gate_s2c_confluence import make_bars, END
from scripts.gate_s3_economic_diagnostic import read_snapshot
from scripts.gate_cost_viability_experiment import DEFAULT_SNAPSHOT, DEFAULT_PROTOCOL, validate_protocol
from app.schemas.market import Candle

STRATEGY = "EMA_TREND_PULLBACK_V1"


def plan(*, side="LONG", tf="15m", target=.5):
    return ResearchPlan(STRATEGY, "1.0.0", "BTCUSDT", tf,
                        END+timedelta(microseconds=1), side, .25, target,
                        "SYNTHETIC_COST_TEST", False, False)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_cost_gate_boundary_is_independent_of_direction_and_does_not_change_brackets(side):
    p = plan(side=side)
    # 100*(two sides*(6 fee + 6 slip) + 1 funding)/10000 = .25
    assert cost.estimated_round_trip_cost(p, 100) == pytest.approx(.25)
    assert cost.filter_cost(p, 100, 2) is p
    assert cost.filter_cost(replace(p, target_distance=.499999), 100, 2).action == "WAIT"
    assert cost.filter_cost(p, 100, 4).action == "WAIT"
    good = replace(p, target_distance=1.)
    assert cost.filter_cost(good, 100, 4) == good
    assert (p.stop_distance, p.target_distance) == (.25, .5)
    assert p.execution_authorized is False


@pytest.mark.parametrize("tf,expected", [("15m", .25), ("1h", .27), ("4h", .36)])
def test_cost_budget_includes_full_frozen_maximum_holding_funding(tf, expected):
    assert cost.estimated_round_trip_cost(plan(tf=tf), 100) == pytest.approx(expected)


@pytest.mark.parametrize("update", [
    {"execution_authorized": True}, {"predictive_edge_proven": True},
    {"strategy_version": "2.0.0"}, {"target_distance": float("nan")},
    {"stop_distance": -1}, {"action": "BUY"},
])
def test_unsafe_or_unregistered_proposals_fail_closed(update):
    with pytest.raises(ValueError, match="Invalid"):
        cost.filter_cost(replace(plan(), **update), 100, 2)


def test_unlisted_thresholds_cannot_be_silently_tuned():
    with pytest.raises(ValueError, match="predeclared"):
        cost.filter_cost(plan(), 100, 3)
    waiting = replace(plan(), action="WAIT", stop_distance=None, target_distance=None)
    assert cost.filter_cost(waiting, 100, 2) is waiting
    with pytest.raises(ValueError, match="Executable WAIT"):
        cost.filter_cost(replace(waiting, execution_authorized=True), 100, 2)


def training(net, trades=5, pf=2):
    return {"completed_trade_net": net, "completed_trades": trades,
            "completed_trade_profit_factor": pf, "all_completed_trades_profitable": False}


def test_selection_must_beat_cash_and_both_available_controls_without_forcing_a_winner():
    records = {"BASELINE": training(-2), "S1": training(3),
               "COST_2X": training(4, trades=4), "COST_4X": training(2),
               "S1_COST_2X": training(3), "S1_COST_4X": training(-1)}
    assert cost.choose_on_training(records) is None
    records["COST_2X"] = training(4)
    assert cost.choose_on_training(records) == "COST_2X"
    records["COST_4X"] = training(4)
    assert cost.choose_on_training(records) == "COST_2X"  # fixed tie, no future lookup
    records["COST_4X"] = training(5, pf=1)
    assert cost.choose_on_training(records) == "COST_2X"


def test_boundary_mark_cannot_create_training_profit_or_sufficient_evidence():
    from app.tests.test_gate_s3_economic_diagnostic import report, trade
    closed = [trade(i*3, -1) for i in range(5)]
    marked = replace(trade(30, 100), exit_reason="END_WINDOW_ESTIMATE")
    r = replace(report(closed+[marked]), estimated_end_window_exits=1)
    stats = cost.metrics(r, Friction(0, 0, 0))
    assert stats["net_including_boundary_marks"] == 95
    assert stats["completed_trade_net"] == -5
    assert stats["completed_trades"] == 5
    assert stats["boundary_marks"] == 1
    assert cost.choose_on_training({"BASELINE": training(-10), "COST_2X": stats}) is None


def test_first_training_selection_is_invariant_to_later_market_returns():
    snapshot, _ = read_snapshot(DEFAULT_SNAPSHOT)
    primary = [Candle.model_validate(b) for b in snapshot["series"]["SOLUSDT"]["1h"][:640]]
    original = cost.DecisionBook(primary)
    changed = list(primary)
    for i in range(400, len(changed)):
        changed[i] = changed[i].model_copy(update={"high": changed[i].high+100})
    other = cost.DecisionBook(changed)
    strategy = "RANGE_BOLLINGER_REVERSION_V1"
    left = cost.walk_forward(original, strategy)[0]
    right = cost.walk_forward(other, strategy)[0]
    assert left["training_arms"] == right["training_arms"]
    assert left["selected_variant"] == right["selected_variant"] == "COST_4X"
    assert left["selection_uses_forward_data"] is False
    assert right["stress_policy_retuned"] is False
    assert set(left["comparisons"]["base"]["arms"]) == {"BASELINE", "COST_4X"}
    assert set(left["comparisons"]["stress"]["arms"]) == {"BASELINE", "COST_4X"}
    for scenario in ("base", "stress"):
        trace = left["comparisons"][scenario]["arm_trade_traces"]
        assert trace["BASELINE"] == trace["COST_4X"]


def test_cost_decision_has_no_access_to_next_open_and_cache_rejects_changed_prefix(monkeypatch):
    rows = make_bars("1h", n=160)
    def synthetic(strategy, prefix, *, as_of, historical_replay):
        assert prefix[-1].closed_at < as_of and historical_replay
        return replace(plan(tf="1h", target=2), cutoff=as_of,
                       stop_distance=1, symbol=prefix[0].symbol)
    monkeypatch.setattr(cost, "evaluate_strategy", synthetic)
    book = cost.DecisionBook(rows)
    cutoff = rows[99].closed_at+timedelta(microseconds=1)
    before = book.evaluate(STRATEGY, rows[:100], cutoff, "COST_2X")
    rows[100] = rows[100].model_copy(update={"open": 9999, "high": 10000})
    assert book.evaluate(STRATEGY, rows[:100], cutoff, "COST_2X") == before
    altered_prefix = list(rows[:100])
    altered_prefix[0] = altered_prefix[0].model_copy(update={"high": 9999})
    with pytest.raises(ValueError, match="prefix"):
        book.evaluate(STRATEGY, altered_prefix, cutoff, "COST_2X")
    with pytest.raises(ValueError, match="clock"):
        book.evaluate(STRATEGY, rows[:100], cutoff+timedelta(seconds=1), "COST_2X")


def test_stress_changes_fills_but_does_not_change_the_gate_estimate(monkeypatch):
    rows = make_bars("1h", n=160)
    def synthetic(strategy, prefix, *, as_of, historical_replay):
        return replace(plan(tf="1h", target=2), cutoff=as_of, stop_distance=1)
    monkeypatch.setattr(cost, "evaluate_strategy", synthetic)
    book = cost.DecisionBook(rows)
    base = book.run(STRATEGY, "COST_2X", 130, 160)
    stressed = book.run(STRATEGY, "COST_2X", 130, 160, friction=cost.STRESS_FRICTION)
    assert base.trades and stressed.trades
    assert base.trades[0].decision_bar_index == stressed.trades[0].decision_bar_index
    assert stressed.trades[0].entry_price > base.trades[0].entry_price
    assert stressed.trades[0].funding_paid > base.trades[0].funding_paid
    assert cost.estimated_round_trip_cost(plan(tf="1h"), 100) == pytest.approx(.27)


def test_frozen_15m_control_accounts_match_archived_experiment_exactly():
    snapshot, _ = read_snapshot(DEFAULT_SNAPSHOT)
    fixture = DEFAULT_SNAPSHOT.parent/"economic-diagnostic.json.gz"
    expected = json.loads(gzip.decompress(fixture.read_bytes()))
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        groups = {tf: tuple(Candle.model_validate(row) for row in rows)
                  for tf, rows in snapshot["series"][symbol].items()}
        book = cost.DecisionBook(groups["15m"], {tf: groups[tf] for tf in ("1h", "4h")})
        for case in (r for r in expected["results"] if r["summary"]["symbol"] == symbol):
            strategy = case["summary"]["strategy"]
            for variant, key in (("BASELINE", "baseline"), ("S1", "filtered")):
                observed = book.run(strategy, variant, 480, 800)
                assert observed.net_pnl == case["summary"][key]["net_pnl"]
                trace = ([r["trade"] for r in case["baseline_trade_trace"]]
                         if variant == "BASELINE" else case["filtered_trade_trace"])
                assert [asdict(t) for t in observed.trades] == trace


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_other_timeframe_controls_preserve_frozen_full_replay_under_stress(tf):
    snapshot, _ = read_snapshot(DEFAULT_SNAPSHOT)
    primary = tuple(Candle.model_validate(b) for b in snapshot["series"]["BTCUSDT"][tf])
    book = cost.DecisionBook(primary)
    for strategy in cost.STRATEGIES:
        original = replay_segment(strategy, primary, start_index=480, end_index=800,
                                  segment="COST_VIABILITY_REUSED_HISTORY", friction=cost.STRESS_FRICTION)
        observed = book.run(strategy, "BASELINE", 480, 800, friction=cost.STRESS_FRICTION)
        assert original == observed


def test_minimum_evidence_and_positive_screen_never_authorize_execution():
    from app.tests.test_gate_s3_economic_diagnostic import report, trade
    class SyntheticBook:
        primary = make_bars("1h", n=520)
        def run(self, strategy, variant, start, end, *, friction=cost.BASE_FRICTION):
            assert end in (400, 520)
            amount = {"BASELINE": -1, "COST_2X": 2, "COST_4X": 1}[variant]
            count = 5 if end == 400 else 30
            return report([trade(i*3, amount) for i in range(count)])
    fold = cost.walk_forward(SyntheticBook(), STRATEGY)[0]
    assert fold["selected_variant"] == "COST_2X"
    assert fold["sufficient_completed_trades_each_arm"]
    assert fold["descriptive_screen_positive"]
    assert fold["profitability_proven"] is False
    assert fold["pristine_holdout"] is False


def test_protocol_rejects_silent_cost_threshold_or_risk_claim_changes():
    p = json.loads(DEFAULT_PROTOCOL.read_text())
    validate_protocol(p)
    for key, value in (("target_to_cost_multiples", [1, 2]),
                       ("stress_gate_estimate", "RETUNE"), ("pristine_holdout", True),
                       ("execution_authorized", True)):
        with pytest.raises(ValueError, match="Protocol"):
            validate_protocol({**p, key: value})

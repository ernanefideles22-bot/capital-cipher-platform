"""Governed queue, causal replay boundaries and deliberately non-operational S5."""
import ast
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from math import sin
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.schemas.market import Candle
from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s5_selection as selection
from app.strategy.gate_classical_s2 import ResearchPlan, STRATEGIES
from app.strategy.gate_s2_replay import Trade


def candles(n=600):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        price = 100 + .02 * i + sin(i / 5)
        stamp = start + timedelta(minutes=15 * (i + 1))
        result.append(Candle(exchange="GATE", symbol="BTCUSDT", timeframe="15m",
                             open=price, high=price + .5, low=price - .5, close=price + .1,
                             volume=100 + i % 40, closed_at=stamp,
                             received_at=stamp + timedelta(seconds=2)))
    return result


@pytest.fixture
def dataset(tmp_path):
    p = tmp_path / "candles.json"
    p.write_text(json.dumps([b.model_dump(mode="json") for b in candles()]))
    return p


def test_preregistered_queue_is_bounded_and_text_cannot_disguise_duplicate(dataset, tmp_path):
    seeds = lab.seed_hypotheses()
    assert len(seeds) == 6
    assert len({h.candidate_id for h in seeds}) == 6
    same = replace(seeds[0], rationale="A differently phrased hypothesis for exactly the same rules.")
    assert same.candidate_id == seeds[0].candidate_id
    for proposals, budget in ((seeds, 5), ((), 6), ((seeds[0], same), 6), (seeds, 13)):
        with pytest.raises(ValueError):
            lab.register_campaign(tmp_path / "invalid", dataset, proposals, budget=budget)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("changes", [
    {"parent_strategy": "exec('order')"}, {"filter_name": "ARBITRARY_CODE"},
    {"threshold": float("nan")}, {"threshold": True}, {"threshold": 1.21},
    {"version": "2"}, {"rationale": "vague"},
])
def test_untrusted_proposals_are_restricted(changes):
    value = asdict(lab.seed_hypotheses()[0])
    with pytest.raises(ValueError):
        lab.Hypothesis(**(value | changes))


def test_registry_preserves_original_and_read_does_not_modify_bytes(dataset, tmp_path):
    root = tmp_path / "lab"
    manifest = lab.register_campaign(root, dataset)
    path = root / "s4-lab.sqlite3"
    original = path.read_bytes()
    report = selection.selection_report(root)
    assert report["registered_trials_including_failed"] == 6
    assert not report["campaign_complete"]
    assert all(c["status"] == "PENDING_EVALUATION" and c["rank"] is None for c in report["candidates"])
    assert manifest["partitions"]["reserved_holdout"] == [480, 600]
    assert path.read_bytes() == original
    with pytest.raises(FileExistsError):
        lab.register_campaign(root, dataset)
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET payload='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")


def test_tamper_is_detected_even_when_trigger_was_removed(dataset, tmp_path):
    root = tmp_path / "lab"
    lab.register_campaign(root, dataset)
    with sqlite3.connect(root / "s4-lab.sqlite3") as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload='{}'")
    with pytest.raises(ValueError, match="integrity"):
        selection.selection_report(root)


def test_dataset_or_engine_change_blocks_before_trial_starts(dataset, tmp_path, monkeypatch):
    root = tmp_path / "lab"
    lab.register_campaign(root, dataset)
    original = dataset.read_text()
    dataset.write_text(original + " ")
    with pytest.raises(ValueError, match="bytes changed"):
        lab.run_campaign(root, dataset)
    assert lab.read_campaign(root)["started"] is False
    dataset.write_text(original)
    monkeypatch.setattr(lab, "engine_fingerprint", lambda: "changed")
    with pytest.raises(ValueError, match="frozen policy"):
        lab.run_campaign(root, dataset)


@pytest.mark.parametrize("change", ["receipt", "gap", "venue", "nonfinite", "too_short"])
def test_input_cannot_fabricate_receipts_or_hide_bad_data(dataset, change):
    values = json.loads(dataset.read_text())
    if change == "receipt":
        del values[0]["received_at"]
    elif change == "gap":
        values[1]["closed_at"] = values[0]["closed_at"]
    elif change == "venue":
        values[0]["exchange"] = "BINANCE"
    elif change == "nonfinite":
        values[0]["volume"] = float("inf")
    else:
        values = values[:200]
    dataset.write_text(json.dumps(values))
    with pytest.raises(ValueError):
        lab.load_dataset(dataset)


def test_hosted_paths_and_symlink_journals_are_refused(tmp_path):
    with pytest.raises(ValueError, match="Hosted"):
        lab.read_campaign("/gate-data/other-lab")
    alias = tmp_path / "alias"
    alias.symlink_to("/gate-data", target_is_directory=True)
    with pytest.raises(ValueError, match="Hosted"):
        lab.read_campaign(alias)
    root = tmp_path / "local"
    root.mkdir()
    (root / "s4-lab.sqlite3").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        lab.read_campaign(root)


def test_failure_and_process_interruption_consume_attempts_and_cannot_retry(dataset, tmp_path, monkeypatch):
    root = tmp_path / "lab"
    lab.register_campaign(root, dataset, lab.seed_hypotheses()[:2])
    def fail(*args):
        raise RuntimeError("fixture failure")
    monkeypatch.setattr(lab, "_evaluate_candidate", fail)
    report = lab.run_campaign(root, dataset)
    assert report["failed_candidates"] == 2 and report["complete"]
    assert selection.selection_report(root)["failed_trials"] == 2
    with pytest.raises(ValueError, match="already started"):
        lab.run_campaign(root, dataset)
    root2 = tmp_path / "interrupted"
    lab.register_campaign(root2, dataset)
    with lab._connect(root2) as db:
        lab._append(db, "STARTED", {})
    assert not lab.read_campaign(root2)["complete"]
    with pytest.raises(ValueError, match="already started"):
        lab.run_campaign(root2, dataset)


def test_two_workers_cannot_consume_the_same_queue(dataset, tmp_path, monkeypatch):
    root = tmp_path / "lab"
    lab.register_campaign(root, dataset, lab.seed_hypotheses()[:1])
    monkeypatch.setattr(lab, "_evaluate_candidate", lambda h, b, s: {
        "candidate_id": h.candidate_id, "error_code": "FIXTURE_NO_EVIDENCE"})
    def run():
        try:
            lab.run_campaign(root, dataset)
            return "finished"
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: run(), range(2))) == ["blocked", "finished"]
    assert len(lab.read_campaign(root)["outcomes"]) == 1


def test_walkforward_purges_and_never_delivers_holdout_to_replay(monkeypatch):
    bars = candles()
    split = lab.partitions(len(bars))
    calls = []
    def replay(strategy, visible, **kwargs):
        start, end = kwargs["start_index"], kwargs["end_index"]
        assert len(visible) == end <= 480
        calls.append((start, end, kwargs["friction"], kwargs["plan_evaluator"]))
        return SimpleNamespace(trades=(), start_equity=1000, estimated_end_window_exits=0)
    monkeypatch.setattr(lab, "replay_segment", replay)
    one = lab._evaluate_candidate(lab.seed_hypotheses()[0], bars, split)
    changed = bars[:480] + [b.model_copy(update={"high": 99999}) for b in bars[480:]]
    two = lab._evaluate_candidate(lab.seed_hypotheses()[0], changed, split)
    assert one == two
    assert len(calls) == 20
    for fold in split["folds"]:
        assert fold["validation"][0] - fold["train"][1] == 25
        assert 25 > max(s.max_hold_bars for s in STRATEGIES.values())


def test_filters_use_visible_prefix_and_delayed_scenario_uses_one_older_bar(monkeypatch):
    bars = candles(100)
    seen = []
    def parent(strategy, visible, *, as_of, historical_replay):
        seen.append(len(visible))
        return ResearchPlan(strategy, "1.0.0", "BTCUSDT", "15m", as_of,
                            "LONG", 1, 2, "fixture")
    monkeypatch.setattr(lab, "evaluate_strategy", parent)
    h = lab.Hypothesis("EMA_TREND_PULLBACK_V1", "RELATIVE_VOLUME_MIN", 1.5,
                       "Participation must exceed the registered threshold.")
    last = bars[-1].model_copy(update={"volume": 1000})
    bars[-1] = last
    now = last.received_at
    assert lab.evaluate_hypothesis(h, bars, now).action == "LONG"
    delayed = lab.evaluate_hypothesis(h, bars, now, delayed=True)
    assert delayed.action == "WAIT" and delayed.stop_distance is None
    assert seen == [100, 99]
    assert delayed.cutoff == now and not delayed.execution_authorized


def test_closed_cost_metrics_have_an_independent_oracle_and_exclude_end_marks():
    def trade(net, fees, funding, reason="STOP"):
        return Trade("X", "LONG", 80, 81, 82, 100, 101, 1, 99, 102,
                     net + fees + funding, fees, funding, net, reason)
    source = SimpleNamespace(trades=(trade(8, 1, 1), trade(-4, 2, 1),
                                    trade(900, 0, 0, "END_WINDOW_ESTIMATE")),
                             start_equity=1000, estimated_end_window_exits=1)
    m = lab.trade_metrics(source)
    assert m["completed_trades"] == 2
    assert m["closed_net_pnl"] == 4 and m["expectancy"] == 2
    assert m["profit_factor"] == 2
    assert m["closed_fees"] == 3 and m["closed_funding"] == 2
    assert m["realized_drawdown_pct"] == pytest.approx(4 / 1008 * 100)
    assert m["excluded_end_window_marks"] == 1


def passing_outcome(candidate_id=None):
    m = {"completed_trades": 40, "closed_net_pnl": 8., "profit_factor": 1.5,
         "realized_drawdown_pct": 2., "largest_win_share": .1,
         "closed_fees": 2., "closed_funding": 1.}
    return {"candidate_id": candidate_id, "folds": [
        {"fold": i, "validation": dict(m), "stress": dict(m), "stress_delayed": dict(m),
         "parent_control": {"closed_net_pnl": 4.}} for i in (1, 2)]}


@pytest.mark.parametrize("scenario", ["validation", "stress", "stress_delayed"])
def test_selection_needs_each_fold_and_scenario_and_never_approves_small_samples(scenario):
    value = passing_outcome()
    assert selection._screen(value)[0] == "DESCRIPTIVE_REVIEW_CANDIDATE"
    value["folds"][1][scenario]["completed_trades"] = 2
    assert selection._screen(value)[0] == "INSUFFICIENT_DEVELOPMENT_EVIDENCE"
    value["folds"][1][scenario]["completed_trades"] = 40
    value["folds"][1][scenario]["closed_net_pnl"] = -1
    assert selection._screen(value)[0] == "REJECTED_DEVELOPMENT_SCREEN"


@pytest.mark.parametrize("field,value", [("profit_factor", None), ("profit_factor", 1.1),
    ("realized_drawdown_pct", 6), ("largest_win_share", .8)])
def test_distribution_risk_and_concentration_block_review(field, value):
    outcome = passing_outcome()
    outcome["folds"][0]["stress"][field] = value
    assert selection._screen(outcome)[0] != "DESCRIPTIVE_REVIEW_CANDIDATE"


def test_positive_ranking_still_cannot_inherit_s3_proof_or_promote(dataset, tmp_path, monkeypatch):
    root = tmp_path / "lab"
    lab.register_campaign(root, dataset, lab.seed_hypotheses()[:2])
    monkeypatch.setattr(lab, "_evaluate_candidate", lambda h, b, s: passing_outcome(h.candidate_id))
    lab.run_campaign(root, dataset)
    r = selection.selection_report(root)
    assert sorted(c["rank"] for c in r["candidates"]) == [1, 2]
    assert r["registered_trials_including_failed"] == 2
    for key in ("execution_authorized", "automatic_promotion_authorized", "profitability_proven",
                "holdout_evaluated", "statistical_significance_established",
                "current_s3_results_transferable_to_s4_candidates"):
        assert r[key] is False


def test_real_replay_pipeline_and_cli_report_only_changes_lab(dataset, tmp_path):
    root = tmp_path / "lab"
    original = dataset.read_bytes()
    lab.register_campaign(root, dataset, lab.seed_hypotheses()[:1])
    campaign = lab.run_campaign(root, dataset)
    assert campaign["complete"] and campaign["failed_candidates"] == 0
    result = subprocess.run([sys.executable, "-m", "scripts.gate_s4_s5_lab", "report",
                             "--lab", str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert not report["execution_authorized"]
    assert not report["holdout_evaluated"]
    assert dataset.read_bytes() == original
    assert set(p.name for p in root.iterdir()) == {"s4-lab.sqlite3"}


def test_hosted_service_does_not_import_new_lab_or_selector():
    app_root = Path(lab.__file__).parents[1]
    for directory in ("demo", "api", "oms", "risk"):
        for path in (app_root / directory).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert "gate_s4" not in (node.module or "")
                    assert "gate_s5" not in (node.module or "")
    for module in (lab, selection):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(v in (node.module or "") for v in ("demo", "oms", "risk.manager", "httpx"))

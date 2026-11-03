"""Independent phase, journal, selection and Monte Carlo precision checks."""
import copy
import json
import math
import sqlite3

import pytest

from app.strategy import gate_s4_playbook_study as study


def metrics(net=10):
    return {s: {"completed_trades": 30, "net_including_boundary_mark": net,
        "closed_net_pnl": net, "profit_factor": 1.3, "largest_win_share": .2,
        "candle_close_drawdown_pct": 2.} for s in study.SCENARIOS}


def trial(name, net=10):
    return {"trial_id": name, "policy_id": name, "symbol": "BTCUSDT", "timeframe": "1h",
            "scenarios": metrics(net)}


def test_selection_uses_worst_training_scenario_and_one_winner_per_market():
    a, b = trial("A", 20), trial("B", 15)
    a["scenarios"]["stress_delayed"]["net_including_boundary_mark"] = 10
    a["validation"] = {"net": 1000000}  # Must never affect selection.
    assert study.select_training([a, b])["selected_trial_ids"] == ["B"]
    a["validation"]["net"] = -1000000
    assert study.select_training([a, b])["selected_trial_ids"] == ["B"]


@pytest.mark.parametrize("metric,value", [("completed_trades", 29), ("closed_net_pnl", 0),
    ("profit_factor", 1.19), ("largest_win_share", .51), ("candle_close_drawdown_pct", 5.01),
    ("net_including_boundary_mark", float("nan"))])
def test_every_training_gate_is_required_in_every_scenario(metric, value):
    t = trial("A")
    t["scenarios"]["stress_delayed"][metric] = value
    result = study.select_training([t])
    assert not result["selected_trial_ids"] and result["training_rejection_reasons"]["A"]


def test_missing_cost_scenario_fails_closed():
    t = trial("A")
    del t["scenarios"]["stress_delayed"]
    assert not study.select_training([t])["selected_trial_ids"]


def test_monte_carlo_bound_matches_independent_small_binomial_oracle():
    for hits in (0, 1, 3, 8):
        upper = study.monte_carlo_upper(hits, 20, .025)
        cdf = sum(math.comb(20, k) * upper ** k * (1 - upper) ** (20 - k)
                  for k in range(hits + 1))
        assert cdf == pytest.approx(.025, abs=1e-12)
    assert study.monte_carlo_upper(101, 9999, .001) == 1
    # Point p=1/10000 would pass the old 54-test threshold; precision bound must not.
    assert study.monte_carlo_upper(0, 9999, .05 / 162) * 54 > .05 or (
        study.monte_carlo_upper(0, 9999, .05 / 162) > 1 / 10000)
    with pytest.raises(ValueError):
        study.monte_carlo_upper(-1, 20, .05)


def register_unavailable(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}")
    rows = [{"symbol": s, "timeframe": t, "status": "UNAVAILABLE"}
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") for t in ("15m", "1h", "4h")]
    monkeypatch.setattr(study, "catalog_rows", lambda _: rows)
    root = tmp_path / "study"
    manifest = study.register(root, catalog)
    return root, manifest


def test_all_attempts_retained_no_validation_reader_on_rejected_training(tmp_path, monkeypatch):
    root, manifest = register_unavailable(tmp_path, monkeypatch)
    monkeypatch.setattr(study, "PlaybookReader", lambda *a, **k: pytest.fail("Future reader constructed"))
    study.run(root)
    report = study.review(root)
    assert report["complete"] and report["training_trials"] == 18 and report["training_failed"] == 18
    assert report["selected_on_train"] == report["validation_trials_evaluated"] == 0
    assert report["family_tests_including_not_selected"] == 54
    assert all(t["status"] == "NOT_EVALUATED" for t in report["statistical_tests"])
    assert all(not report[k] for k in ("final_reserve_evaluated", "execution_authorized", "profitability_proven"))
    with pytest.raises(ValueError, match="already started"):
        study.run(root)
    with pytest.raises(FileExistsError):
        study.register(root, tmp_path / "catalog.json")
    with study._connect(root) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 39
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")


def test_journal_rejects_wrong_phase_and_changed_registered_rules(tmp_path, monkeypatch):
    root, manifest = register_unavailable(tmp_path, monkeypatch)
    with study._connect(root) as db, db:
        study.lab._append(db, "VALIDATION_SKIPPED", {"trial_id": manifest["trials"][0]["trial_id"]})
    with pytest.raises(ValueError, match="Missing study start"):
        study.review(root)
    changed = copy.deepcopy(study.CRITERIA)
    changed["minimum_closed_trades"] = 1
    monkeypatch.setattr(study, "CRITERIA", changed)
    with pytest.raises(ValueError, match="rules/criteria/source changed"):
        study.review(root)


def test_selected_validation_is_after_all_training_and_never_opens_reserved_tail(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}")
    rows = []
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        for timeframe in ("15m", "1h", "4h"):
            folder = tmp_path / f"{symbol}-{timeframe}"
            folder.mkdir()
            (folder / "source.json").write_text("{}")
            rows.append({"symbol": symbol, "timeframe": timeframe, "status": "AVAILABLE",
                "relative_path": f"{folder.name}/candles.json", "sha256": "FIXTURE",
                "source_archive_sha256": study.hashlib.sha256(b"{}").hexdigest()})
    monkeypatch.setattr(study, "catalog_rows", lambda _: rows)
    monkeypatch.setattr(study, "load_real_dataset", lambda _: ([None] * 6000, "FIXTURE"))
    monkeypatch.setattr(study, "PlaybookReader", lambda bars, cap: {"cap": cap})
    calls = []
    def measure(policy, bars, reader, start, end, segment):
        calls.append((reader["cap"], start, end, segment))
        if segment != "TRAIN_ONLY":
            assert sum(c[3] == "TRAIN_ONLY" for c in calls) == 18
        return metrics(20 if policy == "BREAKOUT_RETEST_V1" else 10)
    monkeypatch.setattr(study, "measure", measure)
    root = tmp_path / "study"
    study.register(root, catalog)
    study.run(root)
    assert len(calls) == 36
    assert calls[:18] == [(3600, 220, 3600, "TRAIN_ONLY")] * 18
    assert all(cap == 4800 and (start, end) in ((3625, 4200), (4225, 4800))
               for cap, start, end, _ in calls[18:])
    with study._connect(root) as db:
        events, _ = study._events(db)
    assert sum(k == "VALIDATION_SKIPPED" for k, _ in events) == 9
    assert all(v["policy_id"] == "BREAKOUT_RETEST_V1" for k, v in events if k == "VALIDATION_RESULT")

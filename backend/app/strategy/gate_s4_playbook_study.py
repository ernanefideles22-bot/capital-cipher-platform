"""Registered train selection followed by separate temporal playbook validation."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_statistics as stats
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2_replay import Friction, ResearchRisk
from app.strategy.gate_s4_complete_playbooks import PLAYBOOKS, PlaybookReader, playbook_manifest
from app.strategy.gate_s4_playbook_replay import replay_playbook
from app.strategy.gate_s4_real_source import load_real_dataset
from app.strategy.gate_s4_real_study import catalog_rows, windows
from app.strategy.gate_s4_validation import equity_changes

ENGINE = "S4_COMPLETE_PLAYBOOK_TRAIN_SELECTION_V1"
SCENARIOS = {"nominal": (Friction(), 0), "stress": (Friction(12, 12, 2), 0),
             "stress_delayed": (Friction(12, 12, 2), 1)}
CRITERIA = {"minimum_closed_trades": 30, "minimum_profit_factor": 1.2,
            "maximum_candle_close_drawdown_pct": 5., "maximum_largest_win_share": .5}
FAMILY_TESTS = 54  # 2 policies x 9 series x 3 scenarios, against cash.


def fingerprint():
    names = ("gate_s4_complete_playbooks.py", "gate_s4_playbook_replay.py", "gate_s4_playbook_study.py")
    return lab.digest({"base": lab.engine_fingerprint(), **{
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}})


def _path(root):
    p = lab._db_path(root).with_name("s4-playbooks.sqlite3")
    if p.is_symlink():
        raise ValueError("Research journal symlink refused")
    return p


def _connect(root, readonly=False):
    db = sqlite3.connect(_path(root).as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _events(db):
    events, previous = [], lab.GENESIS
    for expected, (seq, kind, payload, prev, checksum) in enumerate(db.execute(
            "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"), 1):
        if expected > 39 or seq != expected or prev != previous or checksum != lab.digest([prev, kind, payload]):
            raise ValueError("Playbook research journal integrity/budget failure")
        events.append((kind, json.loads(payload)))
        previous = checksum
    if not events or events[0][0] != "REGISTERED":
        raise ValueError("Study registration missing")
    m = events[0][1]
    if (m["engine"] != ENGINE or m["engine_sha256"] != fingerprint()
            or m["playbooks"] != playbook_manifest() or m["criteria"] != CRITERIA
            or m["family_tests"] != FAMILY_TESTS or len(m["trials"]) != 18):
        raise ValueError("Registered rules/criteria/source changed")
    ids = {t["trial_id"] for t in m["trials"]}
    train, validated, selected = set(), set(), False
    for i, (kind, value) in enumerate(events[1:], 1):
        if i == 1:
            if kind != "STARTED":
                raise ValueError("Missing study start")
        elif kind in ("TRAIN_RESULT", "TRAIN_FAILED") and not selected:
            if value["trial_id"] not in ids or value["trial_id"] in train:
                raise ValueError("Repeated/unregistered training attempt")
            train.add(value["trial_id"])
        elif kind == "SELECTION" and not selected and len(train) == 18:
            if not set(value["selected_trial_ids"]) <= ids:
                raise ValueError("Selection contains unregistered rule")
            selected = True
        elif kind in ("VALIDATION_RESULT", "VALIDATION_FAILED", "VALIDATION_SKIPPED") and selected:
            if value["trial_id"] not in ids or value["trial_id"] in validated:
                raise ValueError("Repeated/unregistered validation")
            validated.add(value["trial_id"])
        else:
            raise ValueError("Invalid research phase order")
    return events, previous


def register(root, catalog_path):
    rows = catalog_rows(catalog_path)
    trials = [{"trial_id": lab.digest([r["symbol"], r["timeframe"], p]), "policy_id": p,
               "symbol": r["symbol"], "timeframe": r["timeframe"]} for r in rows for p in PLAYBOOKS]
    manifest = {"engine": ENGINE, "engine_sha256": fingerprint(), "playbooks": playbook_manifest(),
        "criteria": CRITERIA, "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "catalog_path": str(Path(catalog_path).resolve()),
        "catalog_sha256": hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest(),
        "datasets": rows, "trials": trials, "family_tests": FAMILY_TESTS,
        "training_window": "[220,60%)", "validation_windows": "[60%+25,70%),[70%+25,80%)",
        "final_reserve_evaluation_authorized_by_this_runner": False,
        "one_training_winner_per_symbol_timeframe": True,
        "selection_uses_validation": False, "new_parameter_search_budget": 0,
        "statistics": {"blocks": list(stats.BLOCK_LENGTHS), "replications": stats.REPLICATIONS,
                       "seed": stats.SEED, "alpha": stats.ALPHA,
                       "monte_carlo_family_error_budget": .05},
        "risk": asdict(ResearchRisk()), "friction": asdict(Friction()),
        "previous_project_searches_fully_corrected": False,
        "validation_dates_previously_used_in_project_research": True,
        "never_seen_project_holdout": False, "prospective_evidence": False}
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("xb"):
        pass
    with closing(_connect(root)) as db, db:
        db.executescript("""
            CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
        """)
        lab._append(db, "REGISTERED", manifest)
    return manifest


def measure(policy, bars, reader, start, end, segment):
    results = {}
    for name, (friction, delay) in SCENARIOS.items():
        value = replay_playbook(policy, bars[:end], reader, start=start, end=end, segment=segment,
                                friction=friction, risk=ResearchRisk(), extra_delay=delay)
        replay = value["report"]
        results[name] = {**lab.trade_metrics(replay),
            **equity_changes(replay, bars, start=start, end=end, friction=friction),
            "attribution": asdict(attribute(replay, assumptions=friction)),
            "trades": [asdict(t) for t in replay.trades],
            **{k: v for k, v in value.items() if k != "report"}}
    return results


def gate(scenarios):
    if set(scenarios) != set(SCENARIOS):
        return ["INCOMPLETE_COST_SCENARIOS"]
    reasons = []
    for name, m in scenarios.items():
        if any(not isinstance(m[k], (int, float)) or not math.isfinite(m[k]) for k in
               ("completed_trades", "net_including_boundary_mark", "closed_net_pnl", "candle_close_drawdown_pct")):
            reasons.append(name + "_NONFINITE_METRICS")
            continue
        if m["completed_trades"] < CRITERIA["minimum_closed_trades"]:
            reasons.append(name + "_INSUFFICIENT_CLOSED_TRADES")
        if m["net_including_boundary_mark"] <= 0 or m["closed_net_pnl"] <= 0:
            reasons.append(name + "_NONPOSITIVE_NET")
        if m["profit_factor"] is None or m["profit_factor"] < CRITERIA["minimum_profit_factor"]:
            reasons.append(name + "_PROFIT_FACTOR")
        if m["largest_win_share"] is None or m["largest_win_share"] > CRITERIA["maximum_largest_win_share"]:
            reasons.append(name + "_CONCENTRATION_OR_UNDEFINED")
        if m["candle_close_drawdown_pct"] > CRITERIA["maximum_candle_close_drawdown_pct"]:
            reasons.append(name + "_DRAWDOWN")
    return reasons


def select_training(trials):
    eligible, reasons = {}, {}
    for value in trials:
        if "error_code" in value:
            reasons[value["trial_id"]] = [value["error_code"]]
            continue
        why = gate(value["scenarios"])
        reasons[value["trial_id"]] = why
        if not why:
            eligible.setdefault((value["symbol"], value["timeframe"]), []).append(value)
    selected = []
    for values in eligible.values():
        chosen = sorted(values, key=lambda v: (-min(m["net_including_boundary_mark"]
            for m in v["scenarios"].values()), v["policy_id"]))[0]
        selected.append(chosen["trial_id"])
    return {"selected_trial_ids": sorted(selected), "training_rejection_reasons": reasons,
            "selection_uses_validation": False, "execution_authorized": False}


def _append(root, kind, value):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _events(db)
        lab._append(db, kind, value)


def run(root, *, source_root=None):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if len(events) != 1:
            raise ValueError("Study already started; no retry/reset")
        m = events[0][1]
        source = Path(source_root) if source_root else Path(m["catalog_path"]).parent
        loaded = {}
        for r in m["datasets"]:
            if r["status"] == "AVAILABLE":
                path = source / r["relative_path"]
                bars, sha = load_real_dataset(path)
                if (sha != r["sha256"] or hashlib.sha256(path.with_name("source.json").read_bytes()).hexdigest()
                        != r["source_archive_sha256"]):
                    raise ValueError("Frozen data/source changed")
                loaded[r["symbol"], r["timeframe"]] = bars
        lab._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    training, readers = [], {}
    for trial in m["trials"]:
        key = trial["symbol"], trial["timeframe"]
        try:
            if key not in loaded:
                raise ValueError("SOURCE_UNAVAILABLE")
            bars = loaded[key]
            start, end = windows(len(bars))["diagnosis"]
            if key not in readers:
                readers[key] = PlaybookReader(bars, cap=end)
            result = {**trial, "scenarios": measure(trial["policy_id"], bars, readers[key], start, end, "TRAIN_ONLY")}
            kind = "TRAIN_RESULT"
        except Exception as exc:
            result, kind = {**trial, "error_code": "TRAIN_EVIDENCE_UNAVAILABLE", "error_type": type(exc).__name__}, "TRAIN_FAILED"
        training.append(result)
        _append(root, kind, result)
        print(lab.canonical({"phase": "TRAIN", "trial_id": trial["trial_id"], "status": kind}), flush=True)
    selection = select_training(training)
    _append(root, "SELECTION", selection)
    readers = {}
    for trial in m["trials"]:
        if trial["trial_id"] not in selection["selected_trial_ids"]:
            _append(root, "VALIDATION_SKIPPED", {**trial, "reason": "NOT_SELECTED_USING_TRAIN_ONLY"})
            continue
        try:
            key = trial["symbol"], trial["timeframe"]
            bars = loaded[key]
            if key not in readers:
                readers[key] = PlaybookReader(bars, cap=windows(len(bars))["reserved_holdout"][0])
            folds = [{"fold": n, "window": [a, b], "scenarios": measure(trial["policy_id"], bars,
                readers[key], a, b, "POST_SELECTION_VALIDATION")} for n, (a, b) in enumerate(
                    windows(len(bars))["validation"], 1)]
            value, kind = {**trial, "folds": folds}, "VALIDATION_RESULT"
        except Exception as exc:
            value, kind = {**trial, "error_code": "VALIDATION_EVIDENCE_UNAVAILABLE",
                           "error_type": type(exc).__name__}, "VALIDATION_FAILED"
        _append(root, kind, value)


def monte_carlo_upper(hits, replications, delta):
    """One-sided Clopper-Pearson upper bound for Monte Carlo exceedance probability.

    Only small exceedance counts can support a discovery. Larger counts receive
    conservative upper=1, avoiding unstable underflow and unnecessary computation.
    """
    if (type(hits) is not int or type(replications) is not int or not 0 <= hits <= replications
            or replications < 1 or not 0 < delta < 1):
        raise ValueError("Invalid Monte Carlo precision inputs")
    if hits == 0:
        return -math.expm1(math.log(delta) / replications)
    if hits > 100 or hits == replications:
        return 1.
    def cdf(p):
        term = math.exp(replications * math.log1p(-p))
        value = term
        for k in range(hits):
            term *= (replications - k) / (k + 1) * p / (1 - p)
            value += term
        return value
    lower, upper = 0., 1. - 1e-12
    for _ in range(80):
        mid = (lower + upper) / 2
        if cdf(mid) > delta:
            lower = mid
        else:
            upper = mid
    return upper


def review(root):
    with closing(_connect(root, readonly=True)) as db:
        db.execute("BEGIN")
        events, head = _events(db)
    m = events[0][1]
    training = {v["trial_id"]: v for k, v in events if k in ("TRAIN_RESULT", "TRAIN_FAILED")}
    validation = {v["trial_id"]: (k, v) for k, v in events if k.startswith("VALIDATION_")}
    selection = next((v for k, v in events if k == "SELECTION"), {"selected_trial_ids": []})
    tests, candidates = [], []
    delta = .05 / (FAMILY_TESTS * len(stats.BLOCK_LENGTHS))
    for trial in m["trials"]:
        kind, value = validation.get(trial["trial_id"], ("PENDING", {}))
        indices = []
        for scenario in SCENARIOS:
            if kind == "VALIDATION_RESULT":
                diagnostic = stats.mean_diagnostic([f["scenarios"][scenario]["changes_over_initial_equity"]
                                                    for f in value["folds"]])
                precision = max((monte_carlo_upper(round(s["marginal_p_value"] * (stats.REPLICATIONS + 1) - 1),
                    stats.REPLICATIONS, delta) for s in diagnostic.get("sensitivity", [])), default=1.)
            else:
                diagnostic, precision = {"status": "NOT_EVALUATED", "marginal_p_value": 1.}, 1.
            indices.append(len(tests))
            tests.append({"trial_id": trial["trial_id"], "scenario": scenario,
                          "benchmark": "CASH", **diagnostic, "mc_upper_probability": precision})
        tr = training.get(trial["trial_id"])
        why = (["TRAIN_EVIDENCE_UNAVAILABLE"] if tr is None or "error_code" in tr else gate(tr["scenarios"]))
        val_reasons = ([] if kind == "VALIDATION_RESULT" else [kind])
        if kind == "VALIDATION_RESULT":
            for f in value["folds"]:
                val_reasons.extend(f"FOLD_{f['fold']}_" + x for x in gate(f["scenarios"]))
        candidates.append({**trial, "selected_on_train": trial["trial_id"] in selection["selected_trial_ids"],
            "training_reasons": why, "validation_status": kind, "validation_reasons": val_reasons,
            "training": None if tr is None or "error_code" in tr else {s: {k: v for k, v in tr["scenarios"][s].items()
                if k not in ("trades", "changes_over_initial_equity")} for s in SCENARIOS},
            "validation": [] if kind != "VALIDATION_RESULT" else [{"fold": f["fold"], "scenarios": {
                s: {k: v for k, v in f["scenarios"][s].items() if k not in ("trades", "changes_over_initial_equity")}
                for s in SCENARIOS}} for f in value["folds"]], "test_indices": indices})
    point = stats.holm_adjust([t["marginal_p_value"] for t in tests])
    precision = stats.holm_adjust([t["mc_upper_probability"] for t in tests])
    for t, p, upper in zip(tests, point, precision, strict=True):
        t.update(holm_point_p=p, holm_mc_upper_p=upper, conditional_precision_screen_passed=upper <= .05)
    for row in candidates:
        row["prospective_review_candidate"] = (row["selected_on_train"] and not row["validation_reasons"]
            and all(tests[i]["conditional_precision_screen_passed"] for i in row["test_indices"]))
    return {"engine": ENGINE, "manifest": m, "journal_head": head, "training_trials": len(training),
        "training_failed": sum("error_code" in v for v in training.values()),
        "selected_on_train": len(selection["selected_trial_ids"]),
        "validation_trials_evaluated": sum(k == "VALIDATION_RESULT" for k, v in validation.values()),
        "validation_trials_failed": sum(k == "VALIDATION_FAILED" for k, v in validation.values()),
        "complete": len(training) == 18 and len(validation) == 18,
        "family_tests_including_not_selected": len(tests), "candidates": candidates, "statistical_tests": tests,
        "prospective_review_candidates": sum(c["prospective_review_candidate"] for c in candidates),
        "cash_is_the_only_statistical_benchmark": True, "monte_carlo_precision_guard": True,
        "stationarity_assumptions_independently_verified": False,
        "final_reserve_evaluated": False, "prospective_tracking_started": False,
        "profitability_proven": False, "execution_authorized": False, "automatic_promotion_authorized": False}

"""Production-public history: training diagnosis -> frozen revisions -> later test.

One explicitly registered historical experiment. Never imported by Gate runtime.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_statistics as stats
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment
from app.strategy.gate_s4_real_source import ORIGIN, VENUE, load_real_dataset
from app.strategy.gate_s4_revision_engine import CausalParentPlans, Revision, revision_specs
from app.strategy.gate_s4_validation import SCENARIOS, SERIES, equity_changes
from app.strategy.gate_s5_selection import _screen

ENGINE = "S4_REAL_MARKET_CAUSAL_REVISION_STUDY_V1"


def fingerprint():
    names = ("gate_s4_real_study.py", "gate_s4_real_source.py", "gate_s4_revision_engine.py",
             "gate_s4_statistics.py", "gate_s4_validation.py", "gate_s2_loss_diagnostics.py")
    return lab.digest({"base": lab.engine_fingerprint(), **{
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}})


def windows(count):
    if type(count) is not int or not 3000 <= count <= 6000:
        raise ValueError("Unsupported bounded real-market history")
    a, b, c = int(count * .6), int(count * .7), int(count * .8)
    return {"diagnosis": [220, a], "validation": [[a + 25, b], [b + 25, c]],
            "reserved_holdout": [c, count], "purge_bars": 25}


def catalog_rows(path):
    path = Path(path)
    value = json.loads(path.read_bytes())
    if (value["venue"] != VENUE or value["requested_series"] != 9 or len(value["series"]) != 9
            or {(r["symbol"], r["timeframe"]) for r in value["series"]} != SERIES):
        raise ValueError("Complete nine-series production-public catalog required")
    rows = []
    for r in value["series"]:
        row = dict(r)
        if row["status"] == "AVAILABLE":
            expected = f"{row['symbol']}-{row['timeframe']}/candles.json"
            if row["relative_path"] != expected:
                raise ValueError("Unexpected source path")
            bars, sha = load_real_dataset(path.parent / expected)
            if (sha != row["sha256"] or len(bars) != row["candles"]
                    or (bars[0].symbol, bars[0].timeframe) != (row["symbol"], row["timeframe"])):
                raise ValueError("Source catalog mismatch")
            source = path.parent / expected.replace("candles.json", "source.json")
            archive_bytes = source.read_bytes()
            archive = json.loads(archive_bytes)
            if (archive.get("venue") != VENUE or archive["result"].get("sha256") != sha
                    or not archive.get("raw_pages") or any(
                        not p["url"].startswith(ORIGIN + "/api/v4/futures/usdt/")
                        or hashlib.sha256(p["raw_response_utf8"].encode()).hexdigest() != p["response_sha256"]
                        for p in archive["raw_pages"])):
                raise ValueError("Production raw-page provenance/hash mismatch")
            row["source_archive_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
        elif row["status"] != "UNAVAILABLE":
            raise ValueError("Unknown source status")
        rows.append(row)
    return rows


def _groups(report, plans):
    groups = {}
    for trade in report.trades:
        count = trade.decision_bar_index + 1
        f = plans.features(count)
        direction = 1 if trade.side == "LONG" else -1
        aligned = (f["ema200"] is not None and f["ema200_slope8"] is not None
                   and direction * (plans.closes[count - 1] - f["ema200"]) > 0
                   and direction * f["ema200_slope8"] > 0)
        for key in ("EMA200_ALIGNED" if aligned else "EMA200_UNCONFIRMED_OR_AGAINST",
                    "EFFICIENCY_LE_0_25" if f["efficiency20"] <= .25 else "EFFICIENCY_GT_0_25"):
            bucket = groups.setdefault(key, {"trades": 0, "net": 0., "stops": 0})
            bucket["trades"] += 1
            bucket["net"] += trade.net_pnl
            bucket["stops"] += trade.exit_reason.startswith("STOP")
    return groups


def compare_training_sources(bars, prior_values, end):
    # Canonical JSON uses Z; datetime.isoformat uses +00:00. Match instants,
    # never textual timezone spellings, and never inspect validation outcomes.
    prior = {datetime.fromisoformat(v["closed_at"]): v for v in prior_values}
    matched = [(b, prior[b.closed_at]) for b in bars[:end] if b.closed_at in prior]
    return {"status": "TRAINING_OVERLAP_ONLY", "matched_candles": len(matched),
        "equal_close_candles": sum(b.close == v["close"] for b, v in matched),
        "max_abs_close_difference_pct": max((abs(b.close / v["close"] - 1) * 100
                                               for b, v in matched), default=None),
        "equal_ohlc_candles": sum(all(getattr(b, key) == v[key] for key in ("open", "high", "low", "close"))
                                   for b, v in matched)}


def diagnose(catalog_path, prior_root, output):
    """Inspect only the first 60%; do not construct validation features or trades."""
    rows, results, comparison = catalog_rows(catalog_path), [], []
    for row in rows:
        if row["status"] != "AVAILABLE":
            results.append({**row, "diagnosis_status": "SOURCE_UNAVAILABLE"})
            continue
        bars, _ = load_real_dataset(Path(catalog_path).parent / row["relative_path"])
        start, end = windows(len(bars))["diagnosis"]
        plans = CausalParentPlans(bars, cap=end)
        for strategy in sorted(STRATEGIES):
            report = replay_segment(strategy, bars[:end], start_index=start, end_index=end,
                segment="TRAINING_DIAGNOSIS_ONLY", friction=Friction(), risk=ResearchRisk(),
                plan_evaluator=lambda s, p, t: plans.plan(s, p, t))
            results.append({"symbol": row["symbol"], "timeframe": row["timeframe"],
                "strategy": strategy, "diagnosis_status": "AVAILABLE", "window": [start, end],
                "cost_attribution": asdict(attribute(report, assumptions=Friction())),
                "closed_trade_metrics": lab.trade_metrics(report), "entry_context_groups": _groups(report, plans),
                "cost_attribution_is_same_saved_trade_path": True})
            print(lab.canonical({"diagnosed": row["symbol"], "timeframe": row["timeframe"],
                                 "strategy": strategy}), flush=True)
        old = Path(prior_root) / "history" / f"{row['symbol']}-{row['timeframe']}" / "candles.json"
        if not old.is_file():
            comparison.append({"symbol": row["symbol"], "timeframe": row["timeframe"],
                               "status": "PRIOR_TESTNET_CANONICAL_SOURCE_UNAVAILABLE"})
        else:
            comparison.append({"symbol": row["symbol"], "timeframe": row["timeframe"],
                **compare_training_sources(bars, json.loads(old.read_bytes()), end)})
    report = {"engine": ENGINE, "engine_sha256": fingerprint(), "venue": VENUE,
        "dataset_hashes": {r["symbol"] + "/" + r["timeframe"]: r.get("sha256") for r in rows},
        "rows": results, "testnet_comparison": comparison,
        "prior_report_sha256": hashlib.sha256((Path(prior_root) / "financial-statistical-review.json").read_bytes()).hexdigest(),
        "diagnosis_used_first_60_percent_only": True, "validation_evaluated": False,
        "holdout_evaluated": False, "profitability_proven": False, "execution_authorized": False}
    with Path(output).open("x") as f:
        json.dump(report, f, indent=2, allow_nan=False)
    return report


def _path(root):
    p = lab._db_path(root).with_name("s4-real-study.sqlite3")
    if p.is_symlink():
        raise ValueError("Study journal symlink refused")
    return p


def _connect(root, readonly=False):
    db = sqlite3.connect(_path(root).as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=10)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _read(db):
    previous, events = lab.GENESIS, []
    for expected, (seq, kind, payload, prev, checksum) in enumerate(db.execute(
            "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"), 1):
        if expected > 83 or seq != expected or prev != previous or checksum != lab.digest([prev, kind, payload]):
            raise ValueError("Study journal integrity/budget failure")
        events.append((kind, json.loads(payload)))
        previous = checksum
    if not events or events[0][0] != "REGISTERED":
        raise ValueError("Missing study registration")
    m = events[0][1]
    if (m["engine"] != ENGINE or m["engine_sha256"] != fingerprint()
            or len(m["trials"]) != 81 or m["family_tests"] != 486):
        raise ValueError("Frozen study implementation/family changed")
    ids, seen = {t["trial_id"] for t in m["trials"]}, set()
    for i, (kind, value) in enumerate(events[1:], 1):
        if i == 1:
            if kind != "STARTED":
                raise ValueError("Missing study start")
        elif kind not in ("RESULT", "FAILED") or value["trial_id"] not in ids or value["trial_id"] in seen:
            raise ValueError("Repeated or unexpected study trial")
        else:
            seen.add(value["trial_id"])
    return events, previous


def register(root, catalog_path, diagnosis_path):
    diagnosis_bytes = Path(diagnosis_path).read_bytes()
    diagnosis = json.loads(diagnosis_bytes)
    rows = catalog_rows(catalog_path)
    if (diagnosis["engine_sha256"] != fingerprint() or diagnosis["validation_evaluated"]
            or diagnosis["holdout_evaluated"] or diagnosis["dataset_hashes"] != {
                r["symbol"] + "/" + r["timeframe"]: r.get("sha256") for r in rows}):
        raise ValueError("Training diagnosis does not match frozen sources/implementation")
    proposals = [{"kind": "LEGACY_FILTER", "candidate_id": h.candidate_id, "spec": asdict(h)}
                 for h in lab.seed_hypotheses()] + [
                {"kind": "REVISION", "candidate_id": h.candidate_id, "spec": asdict(h)}
                 for h in revision_specs()]
    trials = [{"trial_id": lab.digest([VENUE, r["symbol"], r["timeframe"], h["candidate_id"]]),
               "symbol": r["symbol"], "timeframe": r["timeframe"], **h} for r in rows for h in proposals]
    manifest = {"engine": ENGINE, "engine_sha256": fingerprint(), "venue": VENUE,
        "registered_at_utc": datetime.now(timezone.utc).isoformat(), "datasets": rows,
        "catalog_path": str(Path(catalog_path).resolve()),
        "catalog_sha256": hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest(),
        "diagnosis_sha256": hashlib.sha256(diagnosis_bytes).hexdigest(), "hypotheses": proposals,
        "trials": trials, "family_tests": 486, "prior_testnet_report_sha256": diagnosis["prior_report_sha256"],
        "prior_54_testnet_trials_preserved": True,
        "all_historical_project_searches_corrected": False,
        "calendar_overlap_with_prior_testnet_research_possible": True,
        "never_seen_project_holdout": False,
        "statistics": {"method": "STATIONARY_BOOTSTRAP_HOLM", "blocks": list(stats.BLOCK_LENGTHS),
            "replications": stats.REPLICATIONS, "seed": stats.SEED, "alpha": stats.ALPHA},
        "risk": asdict(ResearchRisk()), "friction": asdict(Friction()),
        "temporal_rule": "DIAGNOSIS_FIRST_60_VALIDATION_60_TO_80_FINAL_20_RESERVED",
        "hypotheses_were_formulated_using_training_diagnosis": True,
        "independently_attested_registration": False}
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


def evaluate(hypothesis, bars, plans):
    folds = []
    for number, (start, end) in enumerate(windows(len(bars))["validation"], 1):
        fold = {"fold": number, "window": [start, end]}
        for scenario, (friction, delayed) in SCENARIOS.items():
            for control in (False, True):
                report = replay_segment(hypothesis.parent_strategy, bars[:end], start_index=start,
                    end_index=end, segment="POST_DIAGNOSIS_VALIDATION", friction=friction,
                    risk=ResearchRisk(), plan_evaluator=lambda s, p, t: plans.plan(
                        s, p, t, hypothesis=None if control else hypothesis, delayed=delayed))
                fold[scenario + ("_parent" if control else "")] = {
                    **lab.trade_metrics(report), **equity_changes(report, bars, start=start, end=end, friction=friction),
                    "attribution": asdict(attribute(report, assumptions=friction))}
        fold["parent_control"] = fold["validation_parent"]
        folds.append(fold)
    return {"folds": folds, "holdout_evaluated": False, "execution_authorized": False}


def run(root, *, source_root=None):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _read(db)
        if len(events) != 1:
            raise ValueError("Study already started; no reset/retry")
        m = events[0][1]
        source_root = Path(source_root) if source_root is not None else Path(m["catalog_path"]).parent
        loaded, planners = {}, {}
        for r in m["datasets"]:
            if r["status"] == "AVAILABLE":
                p = source_root / r["relative_path"]
                bars, sha = load_real_dataset(p)
                if (sha != r["sha256"] or hashlib.sha256(p.with_name("source.json").read_bytes()).hexdigest()
                        != r["source_archive_sha256"]):
                    raise ValueError("Registered production source changed")
                key = r["symbol"], r["timeframe"]
                loaded[key] = bars
                planners[key] = CausalParentPlans(bars, cap=windows(len(bars))["reserved_holdout"][0])
        lab._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    for trial in m["trials"]:
        key = trial["symbol"], trial["timeframe"]
        if key not in loaded:
            kind, value = "FAILED", {**trial, "error_code": "SOURCE_UNAVAILABLE"}
        else:
            try:
                h = (lab.Hypothesis(**trial["spec"]) if trial["kind"] == "LEGACY_FILTER"
                     else Revision(**trial["spec"]))
                value = {**trial, **evaluate(h, loaded[key], planners[key])}
                kind = "RESULT"
            except Exception as exc:
                kind, value = "FAILED", {**trial, "error_code": "EVALUATION_FAILED",
                                         "error_type": type(exc).__name__}
        with closing(_connect(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _read(db)
            lab._append(db, kind, value)
        print(lab.canonical({"symbol": key[0], "timeframe": key[1], "candidate": trial["candidate_id"],
                             "kind": trial["kind"], "status": kind}), flush=True)


def report(root):
    with closing(_connect(root, readonly=True)) as db:
        db.execute("BEGIN")
        events, head = _read(db)
    m = events[0][1]
    outcomes = {v["trial_id"]: v for k, v in events if k in ("RESULT", "FAILED")}
    tests, rows = [], []
    for trial in m["trials"]:
        value = outcomes.get(trial["trial_id"])
        indices = []
        for scenario in SCENARIOS:
            for benchmark in ("CASH", "MATCHED_PARENT"):
                if value is None or "error_code" in value:
                    diagnostic = {"status": "EVIDENCE_UNAVAILABLE", "marginal_p_value": 1.}
                else:
                    folds = []
                    for fold in value["folds"]:
                        returns = fold[scenario]["changes_over_initial_equity"]
                        parent = ([0.] * len(returns) if benchmark == "CASH" else
                                  fold[scenario + "_parent"]["changes_over_initial_equity"])
                        folds.append(tuple(a - b for a, b in zip(returns, parent, strict=True)))
                    diagnostic = stats.mean_diagnostic(folds)
                indices.append(len(tests))
                tests.append({"trial_id": trial["trial_id"], "scenario": scenario,
                              "benchmark": benchmark, **diagnostic})
        status, reasons, worst = ("PENDING", ["NO_RESULT"], None) if value is None else _screen(value)
        folds = [] if value is None or "error_code" in value else [{"fold": f["fold"], "scenarios": {
            key: {k: v for k, v in f[key].items() if k != "changes_over_initial_equity"}
            for key in (*SCENARIOS, *(s + "_parent" for s in SCENARIOS))}} for f in value["folds"]]
        rows.append({**trial, "descriptive_status": status, "reasons": reasons,
                     "worst_closed_net": worst, "folds": folds, "test_indices": indices})
    adjusted = stats.holm_adjust([t["marginal_p_value"] for t in tests])
    for t, p in zip(tests, adjusted, strict=True):
        t["holm_adjusted_p_value"] = p
        t["conditional_screen_passed"] = p <= stats.ALPHA
    complete = len(outcomes) == len(m["trials"])
    failed = sum("error_code" in v for v in outcomes.values())
    for row in rows:
        row["conditional_statistics_passed"] = all(tests[i]["conditional_screen_passed"] for i in row["test_indices"])
        row["historical_screen_passed"] = (complete and not failed
            and row["descriptive_status"] == "DESCRIPTIVE_REVIEW_CANDIDATE"
            and row["conditional_statistics_passed"] and all(
                f["scenarios"][s]["candle_close_drawdown_pct"] <= 5 for f in row["folds"] for s in SCENARIOS))
    return {"engine": ENGINE, "manifest": m, "journal_head": head, "complete": complete,
        "recorded_trials": len(outcomes), "failed_trials": failed, "family_tests": len(tests),
        "candidates": rows, "statistical_tests": tests,
        "historical_survivors": sum(r["historical_screen_passed"] for r in rows),
        "holdout_evaluated": False, "prospective_evidence": False,
        "stationarity_and_mixing_independently_verified": False,
        "all_historical_project_searches_corrected": False,
        "profitability_proven": False, "execution_authorized": False,
        "automatic_promotion_authorized": False}

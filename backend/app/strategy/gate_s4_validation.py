"""Frozen, whole-suite historical/statistical S4 validation, isolated from S3."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_statistics as stats
from app.strategy.gate_classical_s2 import _atr, evaluate_strategy
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment
from app.strategy.gate_s5_selection import _screen

ENGINE = "S4_FROZEN_HISTORICAL_STATISTICAL_SUITE_V1"
SCENARIOS = {"validation": (Friction(), False),
             "stress": (Friction(12, 12, 2), False),
             "stress_delayed": (Friction(12, 12, 2), True)}
SERIES = {(s, tf) for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT") for tf in ("15m", "1h", "4h")}
STAT_POLICY = {"block_lengths": list(stats.BLOCK_LENGTHS), "replications": stats.REPLICATIONS,
               "seed": stats.SEED, "familywise_alpha": stats.ALPHA,
               "marginal_method": "CENTERED_STATIONARY_BOOTSTRAP_WORST_BLOCK_SENSITIVITY",
               "multiplicity": "HOLM_WHOLE_REGISTERED_SUITE",
               "benchmarks": ["CASH", "MATCHED_PARENT"],
               "statistic": "MEAN_CANDLE_CLOSE_NET_EQUITY_CHANGE_OVER_INITIAL_CAPITAL"}


def engine_fingerprint():
    names = ("gate_s4_validation.py", "gate_s4_statistics.py", "gate_s4_public_snapshot.py")
    return lab.digest({"base": lab.engine_fingerprint(), **{
        name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in names}})


def _path(root):
    path = lab._db_path(root).with_name("s4-validation.sqlite3")
    if path.is_symlink():
        raise ValueError("Validation journal must not be a symlink")
    return path


def _connect(root, *, readonly=False):
    path = _path(root)
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                         uri=True, timeout=10)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _read(db):
    rows = db.execute("SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq").fetchall()
    if not rows or len(rows) > 110:
        raise ValueError("Missing/oversized validation journal")
    previous, events = lab.GENESIS, []
    for expected, (seq, kind, payload, prev, checksum) in enumerate(rows, 1):
        if seq != expected or prev != previous or checksum != lab.digest([prev, kind, payload]):
            raise ValueError("Validation journal integrity failure")
        events.append((kind, json.loads(payload)))
        previous = checksum
    if events[0][0] != "REGISTERED":
        raise ValueError("Suite registration missing")
    manifest = events[0][1]
    if (manifest["engine"] != ENGINE or manifest["engine_sha256"] != engine_fingerprint()
            or manifest["statistics"] != STAT_POLICY):
        raise ValueError("Frozen validation implementation/policy changed")
    ids = {trial["trial_id"] for trial in manifest["trials"]}
    if (len(ids) != len(manifest["trials"]) or not 1 <= len(ids) <= 108
            or manifest["family_tests"] != len(ids) * 6):
        raise ValueError("Invalid registered hypothesis family")
    seen = set()
    for i, (kind, value) in enumerate(events[1:], 1):
        if i == 1:
            if kind != "STARTED":
                raise ValueError("Validation start missing")
        elif (kind not in ("RESULT", "FAILED") or value["trial_id"] not in ids
              or value["trial_id"] in seen):
            raise ValueError("Unexpected or repeated validation result")
        else:
            seen.add(value["trial_id"])
    return events, previous


def register_suite(root, catalog_path):
    raw = Path(catalog_path).read_bytes()
    catalog = json.loads(raw)
    if (catalog.get("venue") != "GATE_FUTURES_TESTNET_PUBLIC"
            or catalog.get("requested_series") != 9 or len(catalog["series"]) != 9
            or {(v["symbol"], v["timeframe"]) for v in catalog["series"]} != SERIES):
        raise ValueError("Full nine-series public Testnet catalog required")
    datasets = []
    for value in catalog["series"]:
        row = dict(value)
        row["relative_path"] = f"{row['symbol']}-{row['timeframe']}/candles.json"
        if row["status"] == "AVAILABLE":
            bars, sha = lab.load_dataset(row["path"])
            if (sha != row["sha256"] or len(bars) != row["candles"]
                    or (bars[0].symbol, bars[0].timeframe) != (row["symbol"], row["timeframe"])):
                raise ValueError("Dataset catalog does not match canonical evidence")
            source = Path(row["path"]).with_name("source.json")
            row["source_archive_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        elif row["status"] != "UNAVAILABLE":
            raise ValueError("Unsupported dataset status")
        datasets.append(row)
    proposals = [asdict(h) for h in lab.seed_hypotheses()]
    trials = [{"trial_id": lab.digest([d["symbol"], d["timeframe"], h.candidate_id]),
               "symbol": d["symbol"], "timeframe": d["timeframe"], "hypothesis": asdict(h)}
              for d in datasets for h in lab.seed_hypotheses()]
    manifest = {"engine": ENGINE, "engine_sha256": engine_fingerprint(),
                "parent_source_commit": "9c1a786907f932a56e3b1a45af4797db96b21a75",
                "catalog_sha256": hashlib.sha256(raw).hexdigest(),
                "registered_at_utc": datetime.now(timezone.utc).isoformat(),
                "datasets": datasets, "hypotheses": proposals, "trials": trials,
                "statistics": STAT_POLICY, "family_tests": len(trials) * 6,
                "holdout_fraction_reserved": .2, "purge_bars": lab.PURGE,
                "friction": asdict(Friction()), "risk": asdict(ResearchRisk()),
                "independently_attested_preregistration": False,
                "cross_suite_trial_history_accounted_for": False}
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    with closing(_connect(root)) as db, db:
        db.executescript("""
            CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,
                payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT,'append-only'); END;
        """)
        lab._append(db, "REGISTERED", manifest)
    return manifest


class PrefixPlans:
    """Causal memoization only. Same parent rules, no mutation of S2/S3 globals."""
    def __init__(self, bars):
        self.bars, self.cache = bars, {}

    def plan(self, strategy, prefix, clock, *, hypothesis=None, delayed=False):
        if (not prefix or len(prefix) > int(len(self.bars) * .8)
                or prefix[-1] is not self.bars[len(prefix) - 1]
                or prefix[-1].closed_at > clock):
            raise ValueError("Parent cache requires its original causal development prefix")
        visible = prefix[:-1] if delayed else prefix
        key = strategy, len(visible)
        if key not in self.cache:
            self.cache[key] = evaluate_strategy(strategy, visible,
                as_of=visible[-1].closed_at + timedelta(microseconds=1), historical_replay=True)
        plan = replace(self.cache[key], cutoff=clock)
        if hypothesis is not None:
            if hypothesis.filter_name == "RELATIVE_VOLUME_MIN":
                mean = fmean(b.volume for b in visible[-21:-1])
                accepted = mean > 0 and visible[-1].volume / mean >= hypothesis.threshold
            else:
                accepted = _atr(visible) / visible[-1].close <= hypothesis.threshold
            if not accepted:
                plan = replace(plan, action="WAIT", stop_distance=None, target_distance=None,
                               reasoning="S4 hypothesis abstention: " + hypothesis.filter_name)
        return plan


def equity_changes(report, bars, *, start, end, friction):
    """Reconstruct net hypothetical candle-close equity, including open losses.

    Open estimates include adverse exit slippage, roundtrip fees and funding.
    Forced boundary exits remain in this economic series, unlike the separate
    completed-trade sample counter. It does not measure intrabar drawdown.
    """
    changes, previous, realized, peak, dd = [], 0., 0., report.start_equity, 0.
    closed = set()
    for index in range(start, end):
        mark = 0.
        active = [t for t in report.trades if t.entry_bar_index <= index < t.exit_bar_index]
        if len(active) > 1:
            raise ValueError("Overlapping positions in isolated replay")
        for number, trade in enumerate(report.trades):
            if trade.exit_bar_index <= index and number not in closed:
                realized += trade.net_pnl
                closed.add(number)
        if active:
            t = active[0]
            direction = 1 if t.side == "LONG" else -1
            price = bars[index].close * (1 - direction * friction.slippage_bps_side / 10000)
            elapsed = (bars[index].closed_at - bars[t.entry_bar_index].closed_at).total_seconds()
            funding = (t.entry_price * t.quantity * friction.funding_bps_8h / 10000
                       * max(1, math.ceil(max(0., elapsed) / 28800)))
            fees = (t.entry_price + price) * t.quantity * friction.fee_bps_side / 10000
            mark = (price - t.entry_price) * direction * t.quantity - fees - funding
        net = realized + mark
        changes.append((net - previous) / report.start_equity)
        previous = net
        peak = max(peak, report.start_equity + net)
        dd = max(dd, (1 - (report.start_equity + net) / peak) * 100)
    if not math.isclose(sum(changes) * report.start_equity, report.net_pnl, abs_tol=1e-7):
        raise ValueError("Candle-close equity does not reconcile to replay")
    return {"changes_over_initial_equity": changes, "net_including_boundary_mark": report.net_pnl,
            "candle_close_drawdown_pct": dd, "intrabar_drawdown_measured": False}


def evaluate_trial(hypothesis, bars, plans):
    folds = []
    for number, window in enumerate(lab.partitions(len(bars))["folds"], 1):
        start, end = window["validation"]
        fold = {"fold": number, "window": window}
        for name, (friction, delayed) in SCENARIOS.items():
            for control in (False, True):
                def callback(strategy, prefix, clock):
                    return plans.plan(strategy, prefix, clock, hypothesis=None if control else hypothesis,
                                      delayed=delayed)
                report = replay_segment(hypothesis.parent_strategy, bars[:end], start_index=start,
                                        end_index=end, segment="S4_SUITE_DEVELOPMENT_VALIDATION",
                                        friction=friction, risk=ResearchRisk(), plan_evaluator=callback)
                key = name + ("_parent" if control else "")
                fold[key] = {**lab.trade_metrics(report),
                             **equity_changes(report, bars, start=start, end=end, friction=friction)}
        fold["parent_control"] = fold["validation_parent"]
        folds.append(fold)
    return {"candidate_id": hypothesis.candidate_id, "hypothesis": asdict(hypothesis),
            "folds": folds, "holdout_evaluated": False, "execution_authorized": False}


def run_suite(root, *, source_root=None):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _read(db)
        manifest = events[0][1]
        if len(events) != 1:
            raise ValueError("Suite already started; preserve failed/interrupted attempts")
        # Verify every available source before spending any trial budget.
        loaded = {}
        for dataset in manifest["datasets"]:
            if dataset["status"] == "AVAILABLE":
                path = (Path(dataset["path"]) if source_root is None else
                        Path(source_root) / dataset["relative_path"])
                bars, sha = lab.load_dataset(path)
                source_sha = hashlib.sha256(path.with_name("source.json").read_bytes()).hexdigest()
                if sha != dataset["sha256"] or source_sha != dataset["source_archive_sha256"]:
                    raise ValueError("Registered source evidence changed")
                loaded[dataset["symbol"], dataset["timeframe"]] = bars
        lab._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    planners = {key: PrefixPlans(value) for key, value in loaded.items()}
    for trial in manifest["trials"]:
        key = trial["symbol"], trial["timeframe"]
        if key not in loaded:
            value = {**trial, "error_code": "SOURCE_UNAVAILABLE_REGISTERED_TRIAL"}
            kind = "FAILED"
        else:
            try:
                value = {**trial, **evaluate_trial(lab.Hypothesis(**trial["hypothesis"]),
                                                  loaded[key], planners[key])}
                kind = "RESULT"
            except Exception as exc:
                value = {**trial, "error_code": "EVALUATION_FAILED", "error_type": type(exc).__name__}
                kind = "FAILED"
        with closing(_connect(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _read(db)
            lab._append(db, kind, value)
        print(lab.canonical({"trial": trial["trial_id"], "symbol": key[0], "timeframe": key[1],
                             "status": kind}), flush=True)


def suite_report(root):
    with closing(_connect(root, readonly=True)) as db:
        db.execute("BEGIN")
        events, head = _read(db)
    manifest = events[0][1]
    results = {v["trial_id"]: v for kind, v in events if kind in ("RESULT", "FAILED")}
    tests, candidates = [], []
    coverage = all(d["status"] == "AVAILABLE" for d in manifest["datasets"])
    for trial in manifest["trials"]:
        outcome = results.get(trial["trial_id"])
        indices = []
        for scenario in SCENARIOS:
            for benchmark in ("CASH", "MATCHED_PARENT"):
                if outcome is None or "error_code" in outcome:
                    diagnostic = {"status": "EVIDENCE_UNAVAILABLE", "marginal_p_value": 1.,
                                  "mean_net_excess_per_bar": None, "lower_pointwise_95_mean": None}
                else:
                    folds = []
                    for fold in outcome["folds"]:
                        values = fold[scenario]["changes_over_initial_equity"]
                        control = ([0.] * len(values) if benchmark == "CASH" else
                                   fold[scenario + "_parent"]["changes_over_initial_equity"])
                        folds.append(tuple(a - b for a, b in zip(values, control, strict=True)))
                    diagnostic = stats.mean_diagnostic(folds)
                indices.append(len(tests))
                tests.append({"trial_id": trial["trial_id"], "scenario": scenario,
                              "benchmark": benchmark, **diagnostic})
        if outcome is None:
            status, reasons, score = "PENDING_EVALUATION", ["NO_RESULT"], None
        else:
            status, reasons, score = _screen(outcome)
        summaries = [] if not outcome or "error_code" in outcome else [{
            "fold": fold["fold"], "scenarios": {name: {
                k: v for k, v in fold[name].items() if k != "changes_over_initial_equity"}
                for name in SCENARIOS},
            "matched_parent_net": {name: fold[name + "_parent"]["net_including_boundary_mark"]
                                   for name in SCENARIOS}} for fold in outcome["folds"]]
        candidates.append({**trial, "descriptive_status": status, "reasons": reasons,
                           "worst_closed_net": score, "folds": summaries, "test_indices": indices})
    adjusted = stats.holm_adjust([v["marginal_p_value"] for v in tests])
    for row, p in zip(tests, adjusted, strict=True):
        row["holm_adjusted_p_value"] = p
        row["conditional_multiplicity_screen_passed"] = p <= stats.ALPHA
    complete = len(results) == len(manifest["trials"])
    failed = sum("error_code" in v for v in results.values())
    for row in candidates:
        statistical = all(tests[i]["conditional_multiplicity_screen_passed"] for i in row["test_indices"])
        drawdown = all(v["candle_close_drawdown_pct"] <= 5.
                       for f in row["folds"] for v in f["scenarios"].values())
        row.update(conditional_statistical_screen_passed=statistical,
                   historical_research_screen_passed=(complete and coverage and failed == 0 and drawdown
                       and statistical and row["descriptive_status"] == "DESCRIPTIVE_REVIEW_CANDIDATE"),
                   execution_authorized=False, automatic_promotion_authorized=False)
    return {"model": ENGINE, "manifest": manifest, "journal_head": head,
            "registered_trials": len(manifest["trials"]), "recorded_trials": len(results),
            "failed_trials_including_unavailable_sources": failed, "complete": complete,
            "source_coverage_complete": coverage, "family_tests_including_failed": len(tests),
            "candidates": candidates, "statistical_tests": tests,
            "historical_screen_survivors": sum(c["historical_research_screen_passed"] for c in candidates),
            "bootstrap_assumptions_independently_verified": False,
            "statistical_significance_established": False, "cross_suite_history_adjusted": False,
            "holdout_evaluated": False, "prospective_evidence": False,
            "testnet_results_prove_live_liquidity": False,
            "profitability_proven": False, "execution_authorized": False,
            "automatic_promotion_authorized": False}

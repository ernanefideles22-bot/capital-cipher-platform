"""Pre-register one thesis x three 4h markets, then examine known training only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from app.strategy import gate_s4_lab as lab
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2_replay import ResearchRisk
from app.strategy.gate_s4_playbook_study import CRITERIA, SCENARIOS, gate
from app.strategy.gate_s4_real_source import load_real_dataset
from app.strategy.gate_s4_real_study import catalog_rows
from app.strategy.gate_s4_slow_momentum import POLICY_ID, SYMBOLS, MomentumReader, manifest
from app.strategy.gate_s4_slow_momentum_replay import replay_momentum
from app.strategy.gate_s4_validation import equity_changes

ENGINE = "S4_SLOW_MOMENTUM_KNOWN_TRAIN_ONLY_V1"


def fingerprint():
    names = ("gate_s4_slow_momentum.py", "gate_s4_slow_momentum_replay.py",
        "gate_s4_slow_momentum_study.py", "gate_s4_playbook_study.py", "gate_s4_complete_playbooks.py",
        "gate_s4_real_source.py", "gate_s4_real_study.py", "gate_s4_validation.py",
        "gate_s2_loss_diagnostics.py")
    return lab.digest({"base": lab.engine_fingerprint(), **{
        n: hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}})


def _path(root):
    path = lab._db_path(root).with_name("s4-slow-momentum.sqlite3")
    if path.is_symlink():
        raise ValueError("Journal symlink refused")
    return path


def _connect(root, readonly=False):
    db = sqlite3.connect(_path(root).as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _events(db):
    events, previous = [], lab.GENESIS
    for expected, (seq, kind, payload, prev, sha) in enumerate(db.execute(
            "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"), 1):
        if expected > 5 or seq != expected or prev != previous or sha != lab.digest([prev, kind, payload]):
            raise ValueError("Journal integrity/budget failure")
        events.append((kind, json.loads(payload)))
        previous = sha
    if not events or events[0][0] != "REGISTERED":
        raise ValueError("Registration missing")
    m = events[0][1]
    if (m["engine"] != ENGINE or m["engine_sha256"] != fingerprint()
            or m["hypothesis"] != manifest() or m["criteria"] != CRITERIA
            or m["scenario_budget"] != 9 or len(m["trials"]) != 3):
        raise ValueError("Frozen hypothesis/engine/criteria changed")
    ids, seen = {v["trial_id"] for v in m["trials"]}, set()
    if len(ids) != 3:
        raise ValueError("Three distinct registered markets required")
    for i, (kind, v) in enumerate(events[1:], 1):
        if i == 1:
            if kind != "STARTED":
                raise ValueError("Training start missing")
        elif kind not in ("TRAIN_RESULT", "TRAIN_FAILED") or v["trial_id"] not in ids or v["trial_id"] in seen:
            raise ValueError("Unexpected/repeated training result")
        else:
            seen.add(v["trial_id"])
    return events, previous


def register(root, catalog_path):
    rows = [r for r in catalog_rows(catalog_path) if r["timeframe"] == "4h"]
    if len(rows) != 3 or {r["symbol"] for r in rows} != set(SYMBOLS):
        raise ValueError("All three registered 4h sources required")
    trials = [{"trial_id": lab.digest([POLICY_ID, r["symbol"], "4h"]),
        "policy_id": POLICY_ID, "symbol": r["symbol"], "timeframe": "4h"} for r in rows]
    m = {"engine": ENGINE, "engine_sha256": fingerprint(), "hypothesis": manifest(),
        "registered_at_utc": datetime.now(timezone.utc).isoformat(), "criteria": CRITERIA,
        "catalog_path": str(Path(catalog_path).resolve()),
        "catalog_sha256": hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest(),
        "datasets": rows, "trials": trials, "scenario_budget": 9,
        "scenarios": {n: {"friction": asdict(f), "extra_entry_delay": d} for n, (f, d) in SCENARIOS.items()},
        "risk": asdict(ResearchRisk()), "financial_window": "[220,floor(0.6*N))",
        "prior_project_dates_reused": True, "independent_out_of_sample": False,
        "parameter_grid_budget": 0, "retry_or_reset_allowed": False,
        "later_history_or_reserve_evaluation_authorized": False,
        "fresh_prospective_protocol_required_before_any_validation": True,
        "statistical_validation_performed_by_this_runner": False,
        "execution_authorized": False, "automatic_promotion_authorized": False}
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    with closing(_connect(root)) as db, db:
        db.executescript("""
            CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
        """)
        lab._append(db, "REGISTERED", m)
    return m


def measure(bars, reader, end):
    scenarios = {}
    for name, (friction, delay) in SCENARIOS.items():
        result = replay_momentum(bars[:end], reader, start=220, end=end,
            segment="KNOWN_HISTORY_TRAIN_ONLY", friction=friction, risk=ResearchRisk(), extra_delay=delay)
        report = result["report"]
        scenarios[name] = {**lab.trade_metrics(report),
            **equity_changes(report, bars[:end], start=220, end=end, friction=friction),
            "attribution": asdict(attribute(report, assumptions=friction)),
            "trades": [asdict(t) for t in report.trades],
            **{k: v for k, v in result.items() if k != "report"}}
    return scenarios


def run(root, source_root=None):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if len(events) != 1:
            raise ValueError("Training already started; no retry/reset")
        m = events[0][1]
        source = Path(source_root) if source_root else Path(m["catalog_path"]).parent
        loaded = {}
        for r in m["datasets"]:
            if r["status"] != "AVAILABLE":
                continue
            path = source / r["relative_path"]
            bars, sha = load_real_dataset(path)
            if (sha != r["sha256"] or hashlib.sha256(path.with_name("source.json").read_bytes()).hexdigest()
                    != r["source_archive_sha256"]):
                raise ValueError("Registered data/source changed")
            loaded[r["symbol"]] = bars
        lab._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    for trial in m["trials"]:
        try:
            bars = loaded[trial["symbol"]]
            end = int(.6 * len(bars))
            reader = MomentumReader(bars, cap=end)
            result, kind = {**trial, "window": [220, end], "scenarios": measure(bars, reader, end)}, "TRAIN_RESULT"
        except Exception as exc:
            result, kind = {**trial, "error_code": "TRAIN_EVIDENCE_UNAVAILABLE",
                            "error_type": type(exc).__name__}, "TRAIN_FAILED"
        with closing(_connect(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _events(db)
            lab._append(db, kind, result)
        print(lab.canonical({"symbol": trial["symbol"], "status": kind}), flush=True)


def review(root):
    with closing(_connect(root, readonly=True)) as db:
        db.execute("BEGIN")
        events, head = _events(db)
    rows = []
    for kind, v in events:
        if kind in ("TRAIN_RESULT", "TRAIN_FAILED"):
            reasons = gate(v["scenarios"]) if kind == "TRAIN_RESULT" else [v["error_code"]]
            rows.append({**v, "training_reasons": reasons, "training_screen_passed": not reasons})
    return {"engine": ENGINE, "manifest": events[0][1], "journal_head": head,
        "training_trials": len(rows), "training_failed": sum("error_code" in v for v in rows),
        "training_screen_passed": sum(v["training_screen_passed"] for v in rows),
        "complete": len(rows) == 3, "results": rows,
        "independent_out_of_sample": False, "validation_trials_evaluated": 0,
        "final_reserve_evaluated": False, "statistical_significance_established": False,
        "prospective_tracking_started": False, "profitability_proven": False,
        "execution_authorized": False, "automatic_promotion_authorized": False}

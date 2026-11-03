"""Bounded, offline S4 research. Never imported by the hosted Gate service.

Accepts declarative hypotheses, not arbitrary code. All proposals and development
partitions are frozen before evaluation. Existing S2 rules and S3 stay untouched.
This is a lab foundation, not an autonomous AI or an economic-edge certificate.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import (
    MIN_HISTORY, STRATEGIES, _atr, _check_bars, evaluate_strategy,
)
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment

ENGINE = "S4_BOUNDED_FILTER_LAB_V1"
BASE_COMMIT = "767f9e20d59154b435426876b54ecc4aa5fc758f"
GENESIS = "0" * 64
MAX_BYTES = 8 * 1024 * 1024
MAX_CANDLES = 4096
MAX_CANDIDATES = 12
PURGE = 25  # Longer than every frozen S2 holding horizon (maximum 24 bars).


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def engine_fingerprint() -> str:
    names = ("gate_s4_lab.py", "gate_s5_selection.py", "gate_classical_s2.py", "gate_s2_replay.py")
    return digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in names})


@dataclass(frozen=True)
class Hypothesis:
    parent_strategy: str
    filter_name: str
    threshold: float
    rationale: str
    version: str = "1.0.0"

    def __post_init__(self):
        allowed = {"RELATIVE_VOLUME_MIN": (1.2, 1.5), "ATR_PRICE_MAX": (.01, .02)}
        if (self.parent_strategy not in STRATEGIES or self.version != "1.0.0"
                or self.filter_name not in allowed
                or type(self.threshold) not in (int, float)
                or self.threshold not in allowed[self.filter_name]
                or not isinstance(self.rationale, str)
                or not 20 <= len(self.rationale) <= 1000):
            raise ValueError("Unsupported declarative research hypothesis")

    @property
    def candidate_id(self):
        # Text edits cannot disguise the same tested rules as a new candidate.
        return digest({k: v for k, v in asdict(self).items() if k != "rationale"})


def seed_hypotheses() -> tuple[Hypothesis, ...]:
    """Six explicit baselines for exercising the lab, NOT AI discoveries."""
    return tuple(Hypothesis(parent, name, value, reason)
                 for parent in sorted(STRATEGIES)
                 for name, value, reason in (
                     ("RELATIVE_VOLUME_MIN", 1.2,
                      "Test whether excluding low participation improves net outcomes versus the parent."),
                     ("ATR_PRICE_MAX", .02,
                      "Test whether abstaining in high relative volatility reduces net losses versus the parent.")))


def load_dataset(path: str | Path):
    p = Path(path).resolve()
    if p.stat().st_size > MAX_BYTES:
        raise ValueError("Dataset exceeds offline byte budget")
    raw = p.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("Dataset exceeds offline byte budget")
    values = json.loads(raw)
    if not isinstance(values, list) or not 600 <= len(values) <= MAX_CANDLES:
        raise ValueError("Supply 600..4096 canonical GATE candles")
    if any(not isinstance(v, dict) or not {"exchange", "symbol", "timeframe", "open", "high",
               "low", "close", "volume", "closed_at", "received_at"} <= v.keys() for v in values):
        raise ValueError("Explicit complete OHLCV and receipt timestamps are required")
    bars = tuple(Candle.model_validate(v) for v in values)
    _check_bars(bars, bars[-1].closed_at + timedelta(microseconds=1), replay=True)
    return bars, hashlib.sha256(raw).hexdigest()


def partitions(count: int) -> dict:
    if type(count) is not int or not 600 <= count <= MAX_CANDLES:
        raise ValueError("Unsupported campaign size")
    a, b, holdout = int(count * .4), int(count * .6), int(count * .8)
    return {"index_convention": "HALF_OPEN", "purge_bars": PURGE,
            "folds": [{"train": [MIN_HISTORY, a], "validation": [a + PURGE, b]},
                      {"train": [MIN_HISTORY, b], "validation": [b + PURGE, holdout]}],
            "reserved_holdout": [holdout, count]}


def _db_path(root: str | Path) -> Path:
    root = Path(root).resolve()
    if (root == Path("/gate-data") or Path("/gate-data") in root.parents
            or any("capital-cipher-gate" in p for p in root.parts)):
        raise ValueError("Hosted Gate data paths cannot be used as a laboratory")
    path = root / "s4-lab.sqlite3"
    if path.is_symlink():
        raise ValueError("Lab journal must not be a symlink")
    return path


def _connect(root):
    path = _db_path(root)
    if not path.is_file():
        raise ValueError("Campaign does not exist")
    return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)


def _append(db, kind, payload):
    row = db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    previous = row[0] if row else GENESIS
    encoded = canonical(payload)
    checksum = digest([previous, kind, encoded])
    db.execute("INSERT INTO events(kind,payload,previous_hash,event_hash) VALUES (?,?,?,?)",
               (kind, encoded, previous, checksum))


def _events(db):
    rows = db.execute("SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq").fetchall()
    if not rows or len(rows) > 2 + MAX_CANDIDATES:
        raise ValueError("Missing or oversized campaign journal")
    previous, events = GENESIS, []
    for expected, (seq, kind, payload, prev, checksum) in enumerate(rows, 1):
        if seq != expected or prev != previous or checksum != digest([prev, kind, payload]):
            raise ValueError("Campaign journal integrity failure")
        events.append((kind, json.loads(payload)))
        previous = checksum
    if events[0][0] != "REGISTERED":
        raise ValueError("Campaign registration missing")
    manifest = events[0][1]
    hypotheses = tuple(Hypothesis(**v) for v in manifest["hypotheses"])
    ids = {h.candidate_id for h in hypotheses}
    if (manifest["engine"] != ENGINE or manifest["base_commit"] != BASE_COMMIT
            or manifest["engine_sha256"] != engine_fingerprint()
            or manifest["partitions"] != partitions(manifest["candles"])
            or manifest["risk"] != asdict(ResearchRisk())
            or manifest["friction"] != asdict(Friction())
            or not 1 <= len(hypotheses) <= manifest["budget"] <= MAX_CANDIDATES
            or len(ids) != len(hypotheses)):
        raise ValueError("Campaign manifest violates the frozen policy")
    seen = set()
    for i, (kind, value) in enumerate(events[1:], 1):
        if i == 1:
            if kind != "STARTED":
                raise ValueError("Campaign start missing")
        elif (kind not in {"RESULT", "FAILED"} or value["candidate_id"] not in ids
              or value["candidate_id"] in seen):
            raise ValueError("Unexpected or repeated candidate evaluation")
        else:
            seen.add(value["candidate_id"])
    return events, previous


def register_campaign(root, dataset, hypotheses=None, *, budget=6):
    """Create once. No replacement/reset, no new proposals after registration."""
    bars, sha = load_dataset(dataset)
    proposals = tuple(seed_hypotheses() if hypotheses is None else hypotheses)
    if (type(budget) is not int or not 1 <= len(proposals) <= budget <= MAX_CANDIDATES
            or any(not isinstance(h, Hypothesis) for h in proposals)
            or len({h.candidate_id for h in proposals}) != len(proposals)):
        raise ValueError("Budget exceeded, empty or duplicate proposals")
    manifest = {"engine": ENGINE, "engine_sha256": engine_fingerprint(),
                "base_commit": BASE_COMMIT, "dataset_sha256": sha,
                "candles": len(bars), "symbol": bars[0].symbol, "timeframe": bars[0].timeframe,
                "partitions": partitions(len(bars)), "budget": budget,
                "hypotheses": [asdict(h) for h in proposals],
                "friction": asdict(Friction()), "risk": asdict(ResearchRisk()),
                "registered_at_utc": datetime.now(timezone.utc).isoformat(),
                "prospective_registration_independently_proven": False}
    path = _db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects prior attempts, including interrupted campaigns.
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
        _append(db, "REGISTERED", manifest)
    return manifest


def evaluate_hypothesis(hypothesis, bars, as_of, *, delayed=False):
    visible = bars[:-1] if delayed else bars
    plan = evaluate_strategy(hypothesis.parent_strategy, visible, as_of=as_of,
                             historical_replay=True)
    accepted = True
    if hypothesis.filter_name == "RELATIVE_VOLUME_MIN":
        mean_volume = fmean(b.volume for b in visible[-21:-1])
        accepted = mean_volume > 0 and visible[-1].volume / mean_volume >= hypothesis.threshold
    elif hypothesis.filter_name == "ATR_PRICE_MAX":
        accepted = _atr(visible) / visible[-1].close <= hypothesis.threshold
    if not accepted:
        plan = replace(plan, action="WAIT", stop_distance=None, target_distance=None,
                       reasoning="S4 hypothesis abstention: " + hypothesis.filter_name)
    return plan


def trade_metrics(report):
    """Forced end-window marks are excluded from closed-trade evidence."""
    trades = [t for t in report.trades if t.exit_reason != "END_WINDOW_ESTIMATE"]
    gains = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    equity = peak = report.start_equity
    dd = 0.
    for trade in trades:
        equity += trade.net_pnl
        peak = max(peak, equity)
        dd = max(dd, (1 - equity / peak) * 100)
    net = sum(t.net_pnl for t in trades)
    return {"completed_trades": len(trades), "closed_net_pnl": net,
            "expectancy": net / len(trades) if trades else None,
            "profit_factor": gains / losses if losses else None,
            "closed_fees": sum(t.fees_paid for t in trades),
            "closed_funding": sum(t.funding_paid for t in trades),
            "realized_drawdown_pct": dd,
            "largest_win_share": max((t.net_pnl for t in trades), default=0) / gains if gains else None,
            "excluded_end_window_marks": report.estimated_end_window_exits,
            "slippage_already_in_prices": True}


def _evaluate_candidate(hypothesis, bars, split):
    folds = []
    for number, windows in enumerate(split["folds"], 1):
        outputs = {}
        for label, window, friction, delayed, control in (
                ("train", windows["train"], Friction(), False, False),
                ("validation", windows["validation"], Friction(), False, False),
                ("stress", windows["validation"], Friction(12, 12, 2), False, False),
                ("stress_delayed", windows["validation"], Friction(12, 12, 2), True, False),
                ("parent_control", windows["validation"], Friction(), False, True)):
            start, end = window
            # The evaluator/replay never receives bars beyond this fold's endpoint.
            callback = None if control else (
                lambda _strategy, prefix, clock, d=delayed: evaluate_hypothesis(
                    hypothesis, prefix, clock, delayed=d))
            result = replay_segment(hypothesis.parent_strategy, bars[:end],
                                    start_index=start, end_index=end,
                                    segment=f"S4_FOLD_{number}_{label}",
                                    friction=friction, risk=ResearchRisk(),
                                    plan_evaluator=callback)
            outputs[label] = trade_metrics(result)
        folds.append({"fold": number, "windows": windows, **outputs})
    return {"candidate_id": hypothesis.candidate_id, "hypothesis": asdict(hypothesis),
            "folds": folds, "holdout_evaluated": False,
            "statistical_significance_established": False,
            "execution_authorized": False, "automatic_promotion_authorized": False}


def run_campaign(root, dataset):
    """Consume the frozen queue once. An interrupted run cannot silently restart."""
    bars, sha = load_dataset(dataset)
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        manifest = events[0][1]
        if len(events) != 1:
            raise ValueError("Campaign already started; inspect results, do not reset or retry")
        if sha != manifest["dataset_sha256"]:
            raise ValueError("Dataset bytes changed after registration")
        _append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    for proposal in manifest["hypotheses"]:
        hypothesis = Hypothesis(**proposal)
        try:
            value = _evaluate_candidate(hypothesis, bars, manifest["partitions"])
            kind = "RESULT"
        except Exception:
            # Failed attempts remain in the search denominator; no automatic retry.
            value = {"candidate_id": hypothesis.candidate_id, "error_code": "EVALUATION_FAILED"}
            kind = "FAILED"
        with closing(_connect(root)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            _events(db)
            _append(db, kind, value)
    return read_campaign(root)


def read_campaign(root):
    path = _db_path(root)
    # URI read-only and query_only prevent report reads from mutating lab evidence.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        events, head = _events(db)
    manifest = events[0][1]
    outcomes = [value for kind, value in events if kind in {"RESULT", "FAILED"}]
    return {"manifest": manifest, "journal_head": head,
            "registered_candidates": len(manifest["hypotheses"]),
            "started": len(events) > 1, "outcomes": outcomes,
            "complete": len(outcomes) == len(manifest["hypotheses"]),
            "failed_candidates": sum("error_code" in o for o in outcomes),
            "execution_authorized": False, "automatic_promotion_authorized": False}

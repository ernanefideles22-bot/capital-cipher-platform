"""Bounded append-only research sessions. Never imported by deployed services."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from app.strategy import gate_scenario_staged_levels as observer

MAX_OBSERVATIONS = 256
MAX_SNAPSHOT_BYTES = 512 * 1024


def fingerprint():
    directory = Path(observer.__file__).parent
    paths = [Path(__file__), Path(observer.__file__), directory / "gate_scenario_observer.py",
        directory / "professional_gate_chart_stage1.py", directory / "market_intelligence.py",
        directory.parent / "agents/indicators.py", directory.parent / "schemas/market.py"]
    return observer.digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def _path(root):
    root = Path(root).absolute()
    path = root / "scenario-staged-levels.sqlite3"
    if any(p.is_symlink() for p in (path, root, *root.parents)):
        raise ValueError("Scenario journal symlink refused")
    return path


def _db(root, readonly=False):
    value = sqlite3.connect(_path(root).as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
    if readonly:
        value.execute("PRAGMA query_only=ON")
    return value


def _append(db, kind, value):
    last = db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    previous = last[0] if last else "0" * 64
    payload = observer.canonical(value)
    db.execute("INSERT INTO events(kind,payload,previous_hash,event_hash) VALUES(?,?,?,?)",
               (kind, payload, previous, observer.digest([previous, kind, payload])))


def _events(db):
    out, head = [], "0" * 64
    for seq, kind, payload, previous, sha in db.execute("SELECT * FROM events ORDER BY seq"):
        if seq != len(out) + 1 or previous != head or sha != observer.digest([previous, kind, payload]):
            raise ValueError("Scenario journal chain mismatch")
        out.append((kind, json.loads(payload)))
        head = sha
    if (not out or out[0][0] != "REGISTERED"
            or out[0][1]["source_fingerprint"] != fingerprint()
            or any(k not in {"OBSERVATION", "REJECTED_INPUT"} for k, _ in out[1:])
            or any(k == "REJECTED_INPUT" for k, _ in out[1:-1])
            or len(out) > MAX_OBSERVATIONS + 1):
        raise ValueError("Scenario source, phase or budget mismatch")
    observer.validate_plan(out[0][1]["plan"])
    return out, head


def _bounded(payload):
    if len(observer.canonical(payload).encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Snapshot byte budget exceeded")


def register(root, snapshot, policy=observer.Policy()):
    _bounded(snapshot)
    reading, bar, minute, now = observer.analyze_snapshot(snapshot)
    plan, reason = observer.create_plan(reading, bar.close, policy)
    if plan is None:
        raise ValueError(reason)
    state = observer.initial_state(plan)
    state = observer.advance(plan, state, reading, bar, minute, as_of=now)
    if state["stage"] != "WAIT_TOUCH":
        raise ValueError("Initial snapshot already invalidates plan")
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    value = {"registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": fingerprint(), "plan": plan, "state": state, "snapshot": snapshot,
        "source_provenance": "SUPPLIED_SNAPSHOTS_RECEIPT_CLOCKS_NOT_EXTERNALLY_ATTESTED",
        "prospective_performance_verified": False, "execution_authorized": False}
    with closing(_db(root)) as db, db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,
            payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        _append(db, "REGISTERED", value)
    return review(root)


def _unchanged_history(previous, current):
    for tf in ("15m", "1h", "4h"):
        old = {bar["closed_at"]: bar for bar in previous["candles"][tf]}
        new = {bar["closed_at"]: bar for bar in current["candles"][tf]}
        overlap = set(old) & set(new)
        if not overlap or any(old[t] != new[t] for t in overlap):
            raise ValueError("Observed closed history changed or continuity lost")


def observe(root, snapshot):
    _bounded(snapshot)
    with closing(_db(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if events[-1][0] == "REJECTED_INPUT":
            raise ValueError("Session blocked; preserve it and review the data failure")
        if len(events) - 1 >= MAX_OBSERVATIONS:
            raise ValueError("Scenario observation budget exhausted")
        previous = events[-1][1]
        if observer.digest(previous["snapshot"]) == observer.digest(snapshot):
            # Exact retry has no additional event or candidate emission.
            pass
        else:
            try:
                _unchanged_history(previous["snapshot"], snapshot)
                reading, bar, minute, now = observer.analyze_snapshot(snapshot)
                state = observer.advance(events[0][1]["plan"], previous["state"], reading, bar, minute, as_of=now)
            except (ValueError, KeyError, TypeError) as error:
                _append(db, "REJECTED_INPUT", {"snapshot_hash": observer.digest(snapshot),
                    "error": str(error)[:200], "rejected_at_utc": datetime.now(timezone.utc).isoformat(),
                    "execution_authorized": False})
            else:
                _append(db, "OBSERVATION", {"snapshot": snapshot, "state": state,
                    "technical_annotations": observer.annotations(reading),
                    "candidate_new": previous["state"]["stage"] != "CANDIDATE_ONLY"
                                     and state["stage"] == "CANDIDATE_ONLY",
                    "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
    return review(root)


def review(root):
    with closing(_db(root, readonly=True)) as db:
        events, head = _events(db)
    latest = next(v for _, v in reversed(events) if "state" in v)
    transitions = [{"event": i + 1, "stage": v["state"]["stage"], "reason": v["state"]["reason"],
                    "as_of": v["state"]["last_observed_at"]}
                   for i, (_, v) in enumerate(events) if "state" in v]
    return {"version": observer.VERSION, "journal_head": head,
        "status": "BLOCKED" if events[-1][0] == "REJECTED_INPUT" else latest["state"]["stage"],
        "plan": events[0][1]["plan"], "state": latest["state"], "events": len(events),
        "transitions": transitions, "new_candidate_count": sum(v.get("candidate_new", False) for _, v in events),
        "source_provenance": events[0][1]["source_provenance"],
        "last_error": events[-1][1].get("error") if events[-1][0] == "REJECTED_INPUT" else None,
        "profitability_proven": False, "execution_authorized": False,
        "deployed_service_connected": False}

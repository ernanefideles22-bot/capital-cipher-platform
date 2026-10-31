"""Append-only, tamper-evident offline research journal (never a trading ledger).

Research events are bound by a SHA-256 hash chain and immutable SQLite rows.
The chain detects accidental or unauthorized mutation of the local file when
verified against a separately retained checkpoint. It is NOT cryptographic
proof of who ran a backtest, and is NOT an agent execution permission.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.strategy.research_lab import StrategyHypothesis


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResearchEvent:
    sequence: int
    event_id: str
    kind: str
    hypothesis_hash: str
    previous_hash: str
    event_hash: str
    created_at: str
    payload: dict[str, Any]


class ResearchMemory:
    """Locally persisted experiment memory. No broker credentials are used.

    For a production multi-worker deployment, replace with a database-owned
    append-only store and separately anchored checkpoint hash; do not use
    the container's ephemeral filesystem as your source of truth.
    """

    KINDS = frozenset({
        "HYPOTHESIS_REGISTERED",
        "BASELINE_WALK_FORWARD_OBSERVED",
        "PAPER_CLOSED_TRADE_OBSERVED",
        "RESEARCH_NOTE",
    })

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) == ":memory:":
            raise ValueError("Durable file path required for research memory")
        if self.path.exists() and self.path.is_symlink():
            raise ValueError("Refusing a symbolic link for research memory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS research_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                hypothesis_hash TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS research_events_no_update
                BEFORE UPDATE ON research_events BEGIN
                    SELECT RAISE(ABORT,'research history immutable');
                END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS research_events_no_delete
                BEFORE DELETE ON research_events BEGIN
                    SELECT RAISE(ABORT,'research history immutable');
                END""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @staticmethod
    def _to_event(row: tuple) -> ResearchEvent:
        sequence, event_id, kind, hypothesis_hash, previous_hash, event_hash, created_at, serialized = row
        return ResearchEvent(
            sequence=sequence, event_id=event_id, kind=kind,
            hypothesis_hash=hypothesis_hash, previous_hash=previous_hash,
            event_hash=event_hash, created_at=created_at,
            payload=json.loads(serialized),
        )

    def append(
        self, *, kind: str, hypothesis_hash: str,
        payload: Mapping[str, Any], event_id: str,
    ) -> ResearchEvent:
        if kind not in self.KINDS:
            raise ValueError("Unapproved research event type")
        if not (len(hypothesis_hash) == 64
                and all(ch in "0123456789abcdef" for ch in hypothesis_hash)):
            raise ValueError("Invalid hypothesis fingerprint")
        if not (8 <= len(event_id) <= 180 and
                all(ch.isalnum() or ch in "-_.:" for ch in event_id)):
            raise ValueError("Invalid idempotency identity")
        serialized = _canonical(payload)
        if len(serialized) > 250_000:
            raise ValueError("Research payload too large")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM research_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row is not None:
                existing = self._to_event(row)
                if (existing.kind != kind or
                    existing.hypothesis_hash != hypothesis_hash or
                    _canonical(existing.payload) != serialized):
                    raise ValueError("Idempotency identity conflict")
                return existing
            last = db.execute(
                "SELECT sequence, event_hash FROM research_events "
                "ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous = last[1] if last else "0" * 64
            seq = last[0] + 1 if last else 1
            created_at = datetime.now(timezone.utc).isoformat()
            payload_obj = json.loads(serialized)
            event_hash = _hash({
                "sequence": seq, "event_id": event_id, "kind": kind,
                "hypothesis_hash": hypothesis_hash,
                "previous_hash": previous,
                "created_at": created_at, "payload": payload_obj,
            })
            db.execute("""INSERT INTO research_events
                (sequence,event_id,kind,hypothesis_hash,previous_hash,
                 event_hash,created_at,payload_json)
                VALUES (?,?,?,?,?,?,?,?)""",
                (seq,event_id,kind,hypothesis_hash,previous,event_hash,
                 created_at,serialized))
            return ResearchEvent(seq, event_id, kind, hypothesis_hash,
                                 previous, event_hash, created_at, payload_obj)

    def register(self, hypothesis: StrategyHypothesis) -> ResearchEvent:
        return self.append(
            kind="HYPOTHESIS_REGISTERED",
            hypothesis_hash=hypothesis.fingerprint,
            event_id="hypothesis:" + hypothesis.fingerprint,
            payload=asdict(hypothesis),
        )

    def list_events(self) -> list[ResearchEvent]:
        with self._connect() as db:
            return [
                self._to_event(row)
                for row in db.execute(
                    "SELECT * FROM research_events ORDER BY sequence"
                )
            ]

    def verify_chain(self, *, anchored_head: str | None = None) -> str:
        """Verify continuity and event hashes, then compare external checkpoint.

        Local-only verification cannot detect complete replacement of the
        file; anchor the returned head in a separately managed audit store.
        """
        head = "0" * 64
        expected = 1
        with self._connect() as db:
            for row in db.execute(
                "SELECT * FROM research_events ORDER BY sequence"
            ):
                event = self._to_event(row)
                if event.sequence != expected or event.previous_hash != head:
                    raise ValueError("Research history chain discontinuity")
                actual_hash = _hash({
                    "sequence": event.sequence, "event_id": event.event_id,
                    "kind": event.kind,
                    "hypothesis_hash": event.hypothesis_hash,
                    "previous_hash": event.previous_hash,
                    "created_at": event.created_at, "payload": event.payload,
                })
                if actual_hash != event.event_hash:
                    raise ValueError("Research event integrity failure")
                if not isinstance(event.payload, dict):
                    raise ValueError("Invalid event payload")
                if event.kind not in self.KINDS:
                    raise ValueError("Invalid event kind")
                head = event.event_hash
                expected += 1
        if anchored_head is not None and anchored_head != head:
            raise ValueError("Research head differs from external checkpoint")
        return head

    def paper_closed_observation(
        self, hypothesis: StrategyHypothesis, *,
        paper_order_id: str, decision_id: str, net_pnl: float,
        exit_reason: str, closed_at: datetime, attribution_verified: bool,
    ) -> ResearchEvent:
        """Store observational PAPER result, NEVER claim causation by default."""
        import math
        if not math.isfinite(net_pnl):
            raise ValueError("Non-finite PAPER PnL")
        if not paper_order_id or not decision_id:
            raise ValueError("Order and decision identities required")
        if closed_at.tzinfo is None or closed_at.utcoffset() is None:
            raise ValueError("Closed-at timestamp must be timezone-aware")
        if not exit_reason or len(exit_reason) > 120:
            raise ValueError("Invalid exit reason")
        return self.append(
            kind="PAPER_CLOSED_TRADE_OBSERVED",
            hypothesis_hash=hypothesis.fingerprint,
            event_id="paper:" + _hash({
                "paper_order_id": paper_order_id, "decision_id": decision_id
            }),
            payload={
                "paper_order_id": paper_order_id,
                "decision_id": decision_id,
                "net_pnl": net_pnl,
                "exit_reason": exit_reason,
                "closed_at": closed_at.isoformat(),
                "attribution_verified": attribution_verified,
                "execution_authorized": False,
            },
        )

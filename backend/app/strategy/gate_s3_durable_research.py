"""S3 durable append-only research observations and deterministic cold recovery.

Local, *separate* SQLite file. Never references the existing Gate cycle journal,
research journal, OMS, broker, database service, exchange keys, agent registry or
execution client. No automatic integration or background/continuous execution.

The journal is an auditable local chain, NOT an external tamper-proof signature:
an adversary with write access to the complete SQLite file could rewrite it.
Hash heads, monotonic clocks and immutable SQL triggers detect accidental
corruption, partial changes and invalid replays. Avoid placing secrets in input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_replay import Friction, ResearchRisk
from app.strategy.gate_s3_forward_scorecard import ShadowForwardScorecard

SCHEMA_VERSION = "S3_FORWARD_RESEARCH_V1"
GENESIS_HASH = "0" * 64
MAX_OBSERVATIONS_PER_REPLAY = 100_000
# S2E's publicly derived evidence, with a strict field allowlist.
SHADOW_KEYS = frozenset({
    "status", "symbol", "as_of_utc", "policy_id", "policy_commit",
    "oms_touched", "source_decision_cohort_modified",
    "execution_authorized", "hypotheses",
})
HYPOTHESIS_KEYS = frozenset({
    "strategy_id", "strategy_version", "timeframe", "proposed_action",
    "filtered_action", "stop_distance", "target_distance",
    "execution_authorized", "predictive_edge_proven", "rejection_reasons",
})


class S3JournalBlocked(ValueError):
    """Reject unsafe or ambiguous research storage without touching execution."""


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _event_hash(previous: str, payload_json: str, outcome_json: str) -> str:
    return hashlib.sha256(
        (SCHEMA_VERSION + "\n" + previous + "\n" + payload_json + "\n" + outcome_json).encode("utf-8")
    ).hexdigest()


def _public_shadow(raw: Mapping) -> dict:
    """Whitelist S2E fields; never serialize arbitrary status or private data."""
    if not isinstance(raw, Mapping):
        raise S3JournalBlocked("S2E shadow evidence must be a mapping")
    if not SHADOW_KEYS.issubset(raw):
        raise S3JournalBlocked("Missing frozen S2E provenance fields")
    rows = raw["hypotheses"]
    if not isinstance(rows, list) or len(rows) != len(STRATEGIES):
        raise S3JournalBlocked("Incomplete S2E frozen hypotheses")
    clean = []
    for row in rows:
        if not isinstance(row, Mapping) or not HYPOTHESIS_KEYS.issubset(row):
            raise S3JournalBlocked("Malformed S2E proposal")
        reasons = row["rejection_reasons"]
        if (not isinstance(reasons, list) or len(reasons) > 16
                or any(not isinstance(reason, str) or len(reason) > 96
                       for reason in reasons)):
            raise S3JournalBlocked("Invalid research reason list")
        clean.append({key: row[key] for key in sorted(HYPOTHESIS_KEYS)})
    out = {key: raw[key] for key in sorted(SHADOW_KEYS) if key != "hypotheses"}
    out["hypotheses"] = sorted(clean, key=lambda row: row["strategy_id"])
    return out


def _serialize(candle: Candle, shadow: Mapping, observed_at: datetime) -> str:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise S3JournalBlocked("Naive observation timestamp")
    try:
        return _canonical({
            "schema": SCHEMA_VERSION,
            "candle": candle.model_dump(mode="json"),
            "shadow": _public_shadow(shadow),
            "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        })
    except (TypeError, ValueError, OverflowError) as exc:
        raise S3JournalBlocked("Invalid public S3 event payload") from None


def _deserialize(payload: str) -> tuple[Candle, dict, datetime]:
    try:
        data = json.loads(payload)
        if (not isinstance(data, dict)
                or set(data) != {"schema", "candle", "shadow", "observed_at"}
                or data["schema"] != SCHEMA_VERSION):
            raise ValueError("Unknown research event schema")
        candle = Candle.model_validate(data["candle"])
        shadow = _public_shadow(data["shadow"])
        observed_at = datetime.fromisoformat(data["observed_at"])
        if observed_at.tzinfo is None:
            raise ValueError("Unaware observed_at")
        if _serialize(candle, shadow, observed_at) != payload:
            raise ValueError("Noncanonical event payload")
        return candle, shadow, observed_at
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise S3JournalBlocked("Unreadable durable research event") from None


class DurableS3ForwardResearch:
    """One owned SQLite research journal, one deterministic in-memory scorer.

    A new observation is first evaluated in a temporary copy of the scorer,
    then committed to the journal *before* the active scorer is replaced.
    Crash between commit and replacement is resolved by startup replay.
    Concurrent writers cannot silently interleave: mismatch fails closed.
    """

    def __init__(self, path: str | os.PathLike, *,
                 friction: Friction = Friction(),
                 risk: ResearchRisk = ResearchRisk()):
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise S3JournalBlocked("Research directory must already exist")
        if self.path.is_symlink() or (self.path.exists() and
                not stat.S_ISREG(self.path.stat().st_mode)):
            raise S3JournalBlocked("Research journal must be a regular non-symlink file")
        self._closed = False
        existed = self.path.exists()
        self.db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        try:
            if not existed:
                os.chmod(self.path, 0o600)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self._initialize()
            self.scorer = ShadowForwardScorecard(friction=friction, risk=risk)
            self._head_seq: dict[str,int] = {}
            self._head_hash: dict[str,str] = {}
            self._recover()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self) -> None:
        self.db.execute("""CREATE TABLE IF NOT EXISTS s3_schema (
            name TEXT PRIMARY KEY, version TEXT NOT NULL)""")
        row = self.db.execute(
            "SELECT version FROM s3_schema WHERE name='forward_research'"
        ).fetchone()
        if row is not None and row[0] != SCHEMA_VERSION:
            raise S3JournalBlocked("Incompatible research journal schema")
        self.db.execute(
            "INSERT OR IGNORE INTO s3_schema(name,version) VALUES (?,?)",
            ("forward_research",SCHEMA_VERSION))
        self.db.execute("""CREATE TABLE IF NOT EXISTS s3_observations (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            candle_closed_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            outcome_json TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            UNIQUE(symbol,candle_closed_at))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS s3_heads (
            symbol TEXT PRIMARY KEY,
            last_seq INTEGER NOT NULL,
            event_hash TEXT NOT NULL,
            event_count INTEGER NOT NULL)""")
        self.db.execute("""CREATE TRIGGER IF NOT EXISTS s3_no_event_update
            BEFORE UPDATE ON s3_observations BEGIN
                SELECT RAISE(ABORT,'immutable S3 observation');
            END""")
        self.db.execute("""CREATE TRIGGER IF NOT EXISTS s3_no_event_delete
            BEFORE DELETE ON s3_observations BEGIN
                SELECT RAISE(ABORT,'immutable S3 observation');
            END""")

    def _recover(self) -> None:
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise S3JournalBlocked("SQLite research integrity check failed")
        records = self.db.execute(
            """SELECT seq,symbol,candle_closed_at,observed_at,payload_json,
                      outcome_json,previous_hash,event_hash
               FROM s3_observations ORDER BY seq"""
        ).fetchall()
        if len(records) > MAX_OBSERVATIONS_PER_REPLAY:
            raise S3JournalBlocked("Excessive research replay history")
        heads={}
        previous_clocks={}
        count={}
        restored=ShadowForwardScorecard(
            friction=self.scorer.friction, risk=self.scorer.risk)
        for seq,symbol,closed_at,observed_at,payload,outcome,previous,digest in records:
            last_seq,last_hash=heads.get(symbol,(0,GENESIS_HASH))
            if previous != last_hash or digest != _event_hash(previous,payload,outcome):
                raise S3JournalBlocked("Broken research event hash chain")
            candle,shadow,clock=_deserialize(payload)
            if (candle.symbol != symbol or candle.closed_at.isoformat()!=closed_at
                    or clock.astimezone(timezone.utc).isoformat()!=observed_at):
                raise S3JournalBlocked("Research event identity mismatch")
            if symbol in previous_clocks and clock<=previous_clocks[symbol]:
                raise S3JournalBlocked("Nonmonotonic recovered observation receipts")
            try:
                replay_result=restored.observe(candle,shadow,observed_at=clock)
                if _canonical(replay_result)!=outcome:
                    raise ValueError("Scoring result changed since original write")
            except (ValueError, TypeError, KeyError) as exc:
                raise S3JournalBlocked(
                    "Historical S3 observation cannot be reproduced"
                ) from None
            heads[symbol]=(seq,digest)
            previous_clocks[symbol]=clock
            count[symbol]=count.get(symbol,0)+1
        persisted=self.db.execute(
            "SELECT symbol,last_seq,event_hash,event_count FROM s3_heads"
        ).fetchall()
        if (len(persisted)!=len(heads)
                or any((seq,digest,n)!=(*heads.get(symbol,(-1,"")),count.get(symbol,0))
                       for symbol,seq,digest,n in persisted)):
            raise S3JournalBlocked("Research journal head/row mismatch")
        self.scorer=restored
        self._head_seq={s:seq for s,(seq,h) in heads.items()}
        self._head_hash={s:h for s,(seq,h) in heads.items()}

    def record(self, candle: Candle, shadow: Mapping, *,
               observed_at: datetime) -> dict:
        if self._closed:
            raise S3JournalBlocked("Closed research journal")
        payload=_serialize(candle,shadow,observed_at)
        canonical_candle,canonical_shadow,clock=_deserialize(payload)
        symbol=canonical_candle.symbol
        try:
            self.db.execute("BEGIN IMMEDIATE")
            latest=self.db.execute(
                "SELECT last_seq,event_hash,event_count FROM s3_heads WHERE symbol=?",
                (symbol,)
            ).fetchone()
            expected_seq=self._head_seq.get(symbol,0)
            expected_hash=self._head_hash.get(symbol,GENESIS_HASH)
            if ((latest is None and expected_seq)
                    or (latest is not None and
                        (latest[0]!=expected_seq or latest[1]!=expected_hash))):
                raise S3JournalBlocked("Concurrent research writer detected; restart required")
            duplicate=self.db.execute(
                """SELECT payload_json FROM s3_observations
                   WHERE symbol=? AND candle_closed_at=?""",
                (symbol,canonical_candle.closed_at.isoformat())
            ).fetchone()
            if duplicate:
                if duplicate[0]!=payload or (
                    self.scorer._last.get(symbol)!=canonical_candle.closed_at):
                    raise S3JournalBlocked("Conflicting or out-of-order repeated observation")
                self.db.execute("ROLLBACK")
                return self.scorer.snapshot(symbol,duplicate=True)
            candidate=copy.deepcopy(self.scorer)
            try:
                result=candidate.observe(
                    canonical_candle,canonical_shadow,observed_at=clock)
            except (ValueError,KeyError,TypeError) as exc:
                raise S3JournalBlocked("Rejected unsafe or discontinuous forward observation") from None
            outcome=_canonical(result)
            digest=_event_hash(expected_hash,payload,outcome)
            cursor=self.db.execute(
                """INSERT INTO s3_observations(
                      symbol,candle_closed_at,observed_at,payload_json,
                      outcome_json,previous_hash,event_hash)
                   VALUES (?,?,?,?,?,?,?)""",
                (symbol,canonical_candle.closed_at.isoformat(),
                 clock.astimezone(timezone.utc).isoformat(),payload,
                 outcome,expected_hash,digest))
            count=1 if latest is None else latest[2]+1
            self.db.execute("""INSERT INTO s3_heads(
                 symbol,last_seq,event_hash,event_count) VALUES (?,?,?,?)
                 ON CONFLICT(symbol) DO UPDATE SET
                     last_seq=excluded.last_seq,
                     event_hash=excluded.event_hash,
                     event_count=excluded.event_count""",
                (symbol,cursor.lastrowid,digest,count))
            self.db.execute("COMMIT")
            self.scorer=candidate
            self._head_seq[symbol]=cursor.lastrowid
            self._head_hash[symbol]=digest
            return result
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def status(self) -> dict:
        if self._closed:
            raise S3JournalBlocked("Closed research journal")
        stored=self.db.execute(
            "SELECT symbol,event_count FROM s3_heads ORDER BY symbol"
        ).fetchall()
        return {
            "schema":SCHEMA_VERSION,
            "journal_type":"ISOLATED_FORWARD_RESEARCH",
            "observations":sum(row[1] for row in stored),
            "symbols":{row[0]:row[1] for row in stored},
            "recovered_startup":True,
            "remote_durability_proven":False,
            "external_tamper_proof":False,
            "oms_touched":False,
            "execution_authorized":False,
            "profitability_proven":False,
        }

    def close(self) -> None:
        if not self._closed:
            self.db.close()
            self._closed=True

    def __enter__(self):
        return self

    def __exit__(self,exc_type,exc,trace):
        self.close()

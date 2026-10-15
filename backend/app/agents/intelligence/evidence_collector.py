"""Opt-in, read-only ingestion of existing agent execution traces.

Caller supplies execution IDs and the existing runtime repository. This
module never subscribes to live order events or modifies agent execution.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Protocol

from app.schemas.agents import AgentExecutionTrace
from .shadow_observer import AgentEvidenceSummary, summarize_trace


class TraceReader(Protocol):
    async def load_agent_execution_trace(
        self, execution_id: str,
    ) -> AgentExecutionTrace | None: ...


class EvidenceLedger:
    """Independent durable research store, not the platform's order database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) == ":memory:" or not self.path.parent.is_dir():
            raise ValueError("Supply a persistent path in an existing directory")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_evidence (
                execution_id TEXT PRIMARY KEY,
                agent_name TEXT NOT NULL,
                agent_version TEXT NOT NULL,
                decision_role TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                status TEXT NOT NULL,
                signal TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                evidence_digest TEXT NOT NULL
            )
        """)
        return conn

    def append(self, summary: AgentEvidenceSummary) -> bool:
        values = (
            summary.execution_id, summary.agent_name, summary.agent_version,
            summary.decision_role, summary.symbol, summary.timeframe,
            summary.status, summary.signal, summary.confidence,
            summary.evidence_digest,
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM agent_evidence WHERE execution_id = ?",
                (summary.execution_id,),
            ).fetchone()
            if existing is not None:
                if existing != values:
                    raise ValueError("Conflicting evidence for execution_id")
                return False
            conn.execute(
                "INSERT INTO agent_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        return True

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM agent_evidence").fetchone()[0])


async def collect_completed_traces(
    reader: TraceReader,
    ledger: EvidenceLedger,
    execution_ids: list[str],
    *,
    max_concurrency: int = 8,
) -> tuple[int, int]:
    """Collect terminal traces only; return (new, skipped).

    Missing/nonterminal traces are skipped. Conflicting replay raises.
    Failures never affect the runtime, since this function is called
    independently and does not receive any trading components.
    """
    if not 1 <= max_concurrency <= 32:
        raise ValueError("max_concurrency must be 1..32")
    if len(execution_ids) != len(set(execution_ids)):
        raise ValueError("Duplicate execution IDs")
    semaphore = asyncio.Semaphore(max_concurrency)

    async def read(execution_id: str) -> AgentEvidenceSummary | None:
        async with semaphore:
            trace = await reader.load_agent_execution_trace(execution_id)
            if trace is None or trace.job.status not in ("COMPLETED", "DEAD_LETTER"):
                return None
            if trace.job.execution_id != execution_id:
                raise ValueError("Trace execution ID mismatch")
            return summarize_trace(trace)

    summaries = await asyncio.gather(*(read(i) for i in execution_ids))
    inserted = 0
    skipped = 0
    for summary in summaries:
        if summary is None:
            skipped += 1
        elif ledger.append(summary):
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped

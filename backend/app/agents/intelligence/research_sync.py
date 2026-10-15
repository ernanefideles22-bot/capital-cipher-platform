"""Explicit, read-only sync from existing PAPER runtime repository.

No background scheduler, automatic subscription, order routing, or changes
to runtime semantics. Intended for offline research and integration testing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.schemas.agents import AgentExecutionJob, AgentExecutionTrace
from .evidence_collector import EvidenceLedger, collect_completed_traces


class ResearchRuntimeReader(Protocol):
    async def list_agent_execution_jobs(
        self, *, limit: int = 100,
    ) -> list[AgentExecutionJob]: ...

    async def load_agent_execution_trace(
        self, execution_id: str,
    ) -> AgentExecutionTrace | None: ...


@dataclass(frozen=True)
class ResearchSyncResult:
    inspected: int
    eligible: int
    inserted: int
    skipped: int
    decision_authority: bool = False
    order_authority: bool = False


async def sync_terminal_paper_executions(
    repository: ResearchRuntimeReader,
    ledger: EvidenceLedger,
    *,
    limit: int = 100,
) -> ResearchSyncResult:
    """Observe existing terminal PAPER jobs without altering their state.

    The repository returns the most recent jobs; this is a bounded
    snapshot, not a complete historical backfill or continuous ingestion.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be 1..1000")
    jobs = await repository.list_agent_execution_jobs(limit=limit)
    if len(jobs) > limit:
        raise ValueError("Repository exceeded requested limit")
    eligible = [
        job.execution_id for job in jobs
        if job.execution_mode == "PAPER"
        and job.status in ("COMPLETED", "DEAD_LETTER")
        and job.output is not None
    ]
    if len(eligible) != len(set(eligible)):
        raise ValueError("Duplicate execution IDs in repository result")
    inserted, skipped = await collect_completed_traces(
        repository, ledger, eligible,
    )
    return ResearchSyncResult(
        inspected=len(jobs), eligible=len(eligible),
        inserted=inserted, skipped=skipped,
    )

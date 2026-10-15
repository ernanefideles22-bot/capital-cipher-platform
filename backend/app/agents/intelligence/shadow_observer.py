"""Fail-closed, read-only summaries of existing PAPER agent executions.

No hooks into the OMS or runtime lifecycle. A caller explicitly supplies
completed execution traces; this module never mutates or schedules agents.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from app.schemas.agents import AgentExecutionTrace


@dataclass(frozen=True)
class AgentEvidenceSummary:
    execution_id: str
    agent_name: str
    agent_version: str
    decision_role: str
    symbol: str
    timeframe: str
    status: str
    signal: str
    confidence: int
    evidence_digest: str
    decision_authority: bool = False
    order_authority: bool = False


def summarize_trace(trace: AgentExecutionTrace) -> AgentEvidenceSummary:
    """Summarize terminal PAPER output without exposing raw evidence or secrets."""
    job = trace.job
    if job.execution_mode != "PAPER":
        raise ValueError("Only PAPER execution traces are accepted")
    if job.status not in ("COMPLETED", "DEAD_LETTER") or job.output is None:
        raise ValueError("Only terminal traces with an output can be observed")
    if job.output.agent_name != job.agent_name:
        raise ValueError("Agent identity mismatch")
    # The digest binds the immutable runtime execution ID and terminal
    # outcome, without copying raw market_context, evidence or secrets.
    digest = sha256(
        (job.execution_id + "|" + job.output.model_dump_json(
            exclude={"evidence", "reason", "warnings"}
        )).encode("utf-8")
    ).hexdigest()
    return AgentEvidenceSummary(
        execution_id=job.execution_id,
        agent_name=job.agent_name,
        agent_version=job.agent_version,
        decision_role=job.decision_role,
        symbol=job.input.symbol,
        timeframe=job.input.timeframe,
        status=job.status,
        signal=str(job.output.signal),
        confidence=job.output.confidence,
        evidence_digest=digest,
    )


def summarize_cohort(
    traces: Iterable[AgentExecutionTrace],
    *,
    expected_count: int = 300,
) -> tuple[AgentEvidenceSummary, ...]:
    """Require an exact distinct 300-agent cohort; no partial cohort promotion."""
    if expected_count < 1:
        raise ValueError("expected_count must be positive")
    summaries = tuple(summarize_trace(trace) for trace in traces)
    if len(summaries) != expected_count:
        raise ValueError("Incomplete cohort")
    if len({s.agent_name for s in summaries}) != expected_count:
        raise ValueError("Duplicate agents in cohort")
    if sum(s.decision_role == "PRIMARY" for s in summaries) != 3:
        raise ValueError("Expected exactly 3 PRIMARY agents")
    if sum(s.decision_role == "SHADOW" for s in summaries) != expected_count - 3:
        raise ValueError("Unexpected SHADOW agent count")
    return summaries

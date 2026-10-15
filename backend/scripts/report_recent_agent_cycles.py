"""Print read-only latency aggregates for recent agent execution cycles."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from statistics import median

from sqlalchemy import func, select

from app.core.config import Settings
from app.database.models import AgentExecutionJobModel
from app.database.session import Database


def _milliseconds(seconds: float) -> float:
    return round(seconds * 1_000, 1)


async def report() -> dict:
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("DATABASE_URL is required")

    database = Database(
        settings.database_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
    )
    try:
        async with database.session_factory() as session:
            latest_cycle_ids = (
                select(AgentExecutionJobModel.correlation_id)
                .group_by(AgentExecutionJobModel.correlation_id)
                .order_by(func.max(AgentExecutionJobModel.created_at).desc())
                .limit(6)
            )
            rows = list(
                (
                    await session.execute(
                        select(
                            AgentExecutionJobModel.correlation_id,
                            AgentExecutionJobModel.status,
                            AgentExecutionJobModel.attempt_count,
                            AgentExecutionJobModel.created_at,
                            AgentExecutionJobModel.completed_at,
                        )
                        .where(
                            AgentExecutionJobModel.correlation_id.in_(
                                latest_cycle_ids
                            )
                        )
                        .order_by(AgentExecutionJobModel.created_at.desc())
                    )
                ).all()
            )
    finally:
        await database.dispose()

    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row.correlation_id].append(row)

    cycles = []
    for correlation_id, jobs in grouped.items():
        created = [job.created_at for job in jobs]
        completed = [
            job.completed_at for job in jobs if job.completed_at is not None
        ]
        lifetimes = [
            (job.completed_at - job.created_at).total_seconds()
            for job in jobs
            if job.completed_at is not None
        ]
        cycles.append(
            {
                "correlation_id": correlation_id,
                "jobs": len(jobs),
                "completed": sum(job.status == "COMPLETED" for job in jobs),
                "dead_letter": sum(job.status == "DEAD_LETTER" for job in jobs),
                "first_attempt": sum(
                    job.attempt_count == 1
                    and job.status in {"COMPLETED", "DEAD_LETTER"}
                    for job in jobs
                ),
                "created_span_ms": _milliseconds(
                    (max(created) - min(created)).total_seconds()
                ),
                "completion_span_ms": (
                    _milliseconds((max(completed) - min(completed)).total_seconds())
                    if completed
                    else None
                ),
                "cycle_span_ms": (
                    _milliseconds((max(completed) - min(created)).total_seconds())
                    if completed
                    else None
                ),
                "lifetime_p50_ms": (
                    _milliseconds(median(lifetimes)) if lifetimes else None
                ),
                "first_created_at": min(created).isoformat(),
                "last_completed_at": (
                    max(completed).isoformat() if completed else None
                ),
            }
        )

    cycles.sort(
        key=lambda cycle: datetime.fromisoformat(cycle["first_created_at"]),
        reverse=True,
    )
    return {
        "mode": "READ_ONLY",
        "execution_environment": settings.oms_execution_environment,
        "cycles": cycles[:6],
    }


def main() -> None:
    print(json.dumps(asyncio.run(report()), separators=(",", ":")))


if __name__ == "__main__":
    main()

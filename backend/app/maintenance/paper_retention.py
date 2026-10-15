"""PAPER-only archival retention for high-frequency runtime history.

The runtime database role intentionally has no DELETE grants. This module is
for a separately deployed maintenance worker that connects through the
migration/admin database URL, archives deterministic gzip NDJSON first, and
only then prunes bounded batches of old terminal/published history.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_RETENTION_GUARD_VALUE = "paper-retention-v1"
_TRUE = {"1", "true", "yes", "on"}

_STRICT_AGENT_EVIDENCE_GUARD = """
CREATE OR REPLACE FUNCTION capital_cipher.reject_agent_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
BEGIN
    RAISE EXCEPTION 'agent runtime evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""

_RETENTION_AGENT_EVIDENCE_GUARD = f"""
CREATE OR REPLACE FUNCTION capital_cipher.reject_agent_evidence_mutation()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = ''
AS $function$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting(
            'capital_cipher.paper_retention_authorized', true
       ) = '{_RETENTION_GUARD_VALUE}' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'agent runtime evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _async_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgres://")
    raise ValueError("PAPER retention requires a PostgreSQL database URL")


@dataclass(frozen=True)
class PaperRetentionConfig:
    database_url: str
    archive_root: Path
    hot_hours: int = 24
    archive_days: int = 7
    job_batch_size: int = 1000
    event_batch_size: int = 10000
    max_batches_per_kind: int = 20
    expected_database_host: str | None = None

    @classmethod
    def from_env(cls) -> "PaperRetentionConfig":
        if not _is_true(os.getenv("PAPER_RETENTION_ENABLED")):
            raise RuntimeError("PAPER_RETENTION_ENABLED must be explicitly true")
        if os.getenv("SYSTEM_MODE", "").strip().upper() != "PAPER":
            raise RuntimeError("PAPER retention refuses non-PAPER SYSTEM_MODE")
        if os.getenv("OMS_EXECUTION_ENVIRONMENT", "").strip().upper() != "PAPER":
            raise RuntimeError(
                "PAPER retention refuses non-PAPER OMS_EXECUTION_ENVIRONMENT"
            )
        if _is_true(os.getenv("OMS_TESTNET_ENABLED")):
            raise RuntimeError("PAPER retention refuses TESTNET-enabled environments")
        railway_environment = os.getenv("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
        if railway_environment and railway_environment != "staging":
            raise RuntimeError("PAPER retention is restricted to Railway staging")

        database_url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("MIGRATION_DATABASE_URL is required")
        archive_root_raw = os.getenv("PAPER_RETENTION_ARCHIVE_ROOT", "/archive")
        archive_root = Path(archive_root_raw).expanduser().resolve()
        if not archive_root.is_absolute() or str(archive_root) in {"/", "/tmp"}:
            raise RuntimeError("PAPER_RETENTION_ARCHIVE_ROOT must be a durable path")

        config = cls(
            database_url=_async_database_url(database_url),
            archive_root=archive_root,
            hot_hours=int(os.getenv("PAPER_RETENTION_HOT_HOURS", "24")),
            archive_days=int(os.getenv("PAPER_RETENTION_ARCHIVE_DAYS", "7")),
            job_batch_size=int(os.getenv("PAPER_RETENTION_JOB_BATCH", "1000")),
            event_batch_size=int(os.getenv("PAPER_RETENTION_EVENT_BATCH", "10000")),
            max_batches_per_kind=int(
                os.getenv("PAPER_RETENTION_MAX_BATCHES", "20")
            ),
            expected_database_host=(
                os.getenv("PAPER_RETENTION_EXPECTED_DATABASE_HOST", "").strip()
                or None
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not 6 <= self.hot_hours <= 168:
            raise ValueError("PAPER retention hot window must be 6..168 hours")
        if not 1 <= self.archive_days <= 90:
            raise ValueError("PAPER retention archive window must be 1..90 days")
        if not 100 <= self.job_batch_size <= 5000:
            raise ValueError("PAPER retention job batch must be 100..5000")
        if not 1000 <= self.event_batch_size <= 50000:
            raise ValueError("PAPER retention event batch must be 1000..50000")
        if not 1 <= self.max_batches_per_kind <= 100:
            raise ValueError("PAPER retention max batches must be 1..100")
        host = make_url(self.database_url).host
        if self.expected_database_host and host != self.expected_database_host:
            raise ValueError(
                "PAPER retention database host does not match the expected staging host"
            )


@dataclass(frozen=True)
class ArchiveArtifact:
    kind: str
    path: Path
    sha256: str
    record_count: int
    stored_bytes: int


class PaperRetentionService:
    """Archive then prune high-frequency PAPER history in bounded batches."""

    def __init__(self, engine: AsyncEngine, config: PaperRetentionConfig) -> None:
        self._engine = engine
        self._config = config
        self._config.archive_root.mkdir(parents=True, exist_ok=True)

    async def run(self) -> dict[str, Any]:
        await self._verify_admin_connection()
        started_at = datetime.now(timezone.utc)
        cutoff = started_at - timedelta(hours=self._config.hot_hours)
        summary: dict[str, Any] = {
            "started_at": started_at.isoformat(),
            "cutoff": cutoff.isoformat(),
            "hot_hours": self._config.hot_hours,
            "archive_days": self._config.archive_days,
            "jobs_archived": 0,
            "events_archived": 0,
            "artifacts": [],
        }

        for _ in range(self._config.max_batches_per_kind):
            artifact = await self._archive_and_prune_jobs(cutoff)
            if artifact is None:
                break
            summary["jobs_archived"] += artifact.record_count
            summary["artifacts"].append(self._artifact_summary(artifact))
            if artifact.record_count < self._config.job_batch_size:
                break

        for _ in range(self._config.max_batches_per_kind):
            artifact = await self._archive_and_prune_events(cutoff)
            if artifact is None:
                break
            summary["events_archived"] += artifact.record_count
            summary["artifacts"].append(self._artifact_summary(artifact))
            if artifact.record_count < self._config.event_batch_size:
                break

        summary["archive_files_pruned"] = await asyncio.to_thread(
            self._prune_expired_archives,
            started_at,
        )
        async with self._engine.connect() as conn:
            db_size = await conn.scalar(
                text("SELECT pg_database_size(current_database())")
            )
        summary["database_bytes"] = int(db_size or 0)
        summary["completed_at"] = datetime.now(timezone.utc).isoformat()
        await self._record_audit(summary)
        return summary

    async def _verify_admin_connection(self) -> None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT
                            current_user AS current_user,
                            session_user AS session_user,
                            has_table_privilege(
                                current_user,
                                'capital_cipher.agent_memory_entries',
                                'DELETE'
                            ) AS can_delete_memory,
                            has_table_privilege(
                                current_user,
                                'capital_cipher.event_journal',
                                'DELETE'
                            ) AS can_delete_journal
                        """
                    )
                )
            ).mappings().one()
        if row["current_user"] == "capital_cipher_app":
            raise RuntimeError("PAPER retention refuses the application login")
        if not row["can_delete_memory"] or not row["can_delete_journal"]:
            raise RuntimeError("PAPER retention requires the migration/admin connection")

    async def _archive_and_prune_jobs(
        self, cutoff: datetime
    ) -> ArchiveArtifact | None:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT
                            j.execution_id,
                            jsonb_build_object(
                                'job', to_jsonb(j),
                                'attempts', COALESCE((
                                    SELECT jsonb_agg(to_jsonb(a) ORDER BY a.row_id)
                                    FROM capital_cipher.agent_execution_attempts a
                                    WHERE a.execution_id = j.execution_id
                                ), '[]'::jsonb),
                                'memory_entries', COALESCE((
                                    SELECT jsonb_agg(to_jsonb(m) ORDER BY m.row_id)
                                    FROM capital_cipher.agent_memory_entries m
                                    WHERE m.execution_id = j.execution_id
                                ), '[]'::jsonb)
                            ) AS document
                        FROM capital_cipher.agent_execution_jobs j
                        WHERE j.status IN ('COMPLETED', 'DEAD_LETTER')
                          AND j.completed_at IS NOT NULL
                          AND j.completed_at < :cutoff
                        ORDER BY j.completed_at, j.execution_id
                        LIMIT :batch_size
                        """
                    ),
                    {
                        "cutoff": cutoff,
                        "batch_size": self._config.job_batch_size,
                    },
                )
            ).mappings().all()
        if not rows:
            return None

        documents = [row["document"] for row in rows]
        execution_ids = [str(row["execution_id"]) for row in rows]
        artifact = await asyncio.to_thread(
            self._write_archive,
            "agent-runtime",
            documents,
            cutoff,
        )

        async with self._engine.begin() as conn:
            # PostgreSQL DDL is transactional. The relaxed trigger body exists
            # only inside this maintenance transaction and is restored before
            # commit; any failure rolls the function change and deletes back.
            await conn.execute(text(_RETENTION_AGENT_EVIDENCE_GUARD))
            await conn.execute(
                text(
                    "SET LOCAL capital_cipher.paper_retention_authorized = "
                    f"'{_RETENTION_GUARD_VALUE}'"
                )
            )
            deleted_memory = await conn.execute(
                text(
                    "DELETE FROM capital_cipher.agent_memory_entries "
                    "WHERE execution_id = ANY(CAST(:ids AS varchar[]))"
                ),
                {"ids": execution_ids},
            )
            deleted_attempts = await conn.execute(
                text(
                    "DELETE FROM capital_cipher.agent_execution_attempts "
                    "WHERE execution_id = ANY(CAST(:ids AS varchar[]))"
                ),
                {"ids": execution_ids},
            )
            deleted_jobs = await conn.execute(
                text(
                    "DELETE FROM capital_cipher.agent_execution_jobs "
                    "WHERE execution_id = ANY(CAST(:ids AS varchar[])) "
                    "AND status IN ('COMPLETED', 'DEAD_LETTER')"
                ),
                {"ids": execution_ids},
            )
            if deleted_jobs.rowcount != len(execution_ids):
                raise RuntimeError(
                    "PAPER retention job delete count changed after archival"
                )
            await conn.execute(text(_STRICT_AGENT_EVIDENCE_GUARD))
        print(
            "PAPER_RETENTION_JOBS",
            json.dumps(
                {
                    "jobs": deleted_jobs.rowcount,
                    "attempts": deleted_attempts.rowcount,
                    "memory": deleted_memory.rowcount,
                    "artifact": self._artifact_summary(artifact),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return artifact

    async def _archive_and_prune_events(
        self, cutoff: datetime
    ) -> ArchiveArtifact | None:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT
                            j.event_id,
                            jsonb_build_object(
                                'journal', to_jsonb(j),
                                'outbox', to_jsonb(o)
                            ) AS document
                        FROM capital_cipher.event_journal j
                        JOIN capital_cipher.event_outbox o
                          ON o.event_id = j.event_id
                        WHERE j.created_at < :cutoff
                          AND o.published_at IS NOT NULL
                        ORDER BY j.created_at, j.event_id
                        LIMIT :batch_size
                        """
                    ),
                    {
                        "cutoff": cutoff,
                        "batch_size": self._config.event_batch_size,
                    },
                )
            ).mappings().all()
        if not rows:
            return None

        documents = [row["document"] for row in rows]
        event_ids = [str(row["event_id"]) for row in rows]
        artifact = await asyncio.to_thread(
            self._write_archive,
            "event-journal",
            documents,
            cutoff,
        )

        async with self._engine.begin() as conn:
            deleted = await conn.execute(
                text(
                    """
                    DELETE FROM capital_cipher.event_journal
                    WHERE event_id = ANY(CAST(:ids AS varchar[]))
                      AND created_at < :cutoff
                      AND EXISTS (
                          SELECT 1
                          FROM capital_cipher.event_outbox o
                          WHERE o.event_id = capital_cipher.event_journal.event_id
                            AND o.published_at IS NOT NULL
                      )
                    """
                ),
                {"ids": event_ids, "cutoff": cutoff},
            )
            if deleted.rowcount != len(event_ids):
                raise RuntimeError(
                    "PAPER retention event delete count changed after archival"
                )
        print(
            "PAPER_RETENTION_EVENTS",
            json.dumps(
                {
                    "events": deleted.rowcount,
                    "artifact": self._artifact_summary(artifact),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return artifact

    def _write_archive(
        self,
        kind: str,
        records: list[dict[str, Any]],
        cutoff: datetime,
    ) -> ArchiveArtifact:
        header = {
            "archive_schema_version": "1.0",
            "kind": kind,
            "record_count": len(records),
            "cutoff": cutoff.isoformat(),
        }
        lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
        lines.extend(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in records
        )
        uncompressed = ("\n".join(lines) + "\n").encode("utf-8")
        digest = hashlib.sha256(uncompressed).hexdigest()
        stored = gzip.compress(uncompressed, compresslevel=6, mtime=0)
        relative = Path(
            kind,
            f"{cutoff.year:04d}",
            f"{cutoff.month:02d}",
            f"{cutoff.day:02d}",
            f"{len(records)}-{digest}.ndjson.gz",
        )
        destination = (self._config.archive_root / relative).resolve()
        if not destination.is_relative_to(self._config.archive_root):
            raise RuntimeError("PAPER retention archive path escaped its root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("xb") as stream:
                    stream.write(stored)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return ArchiveArtifact(
            kind=kind,
            path=destination,
            sha256=digest,
            record_count=len(records),
            stored_bytes=len(stored),
        )

    def _prune_expired_archives(self, now: datetime) -> int:
        cutoff = now.timestamp() - (self._config.archive_days * 86400)
        removed = 0
        for path in self._config.archive_root.rglob("*.ndjson.gz"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed

    async def _record_audit(self, summary: dict[str, Any]) -> None:
        audit_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        payload = json.dumps(summary, sort_keys=True, separators=(",", ":"))
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO capital_cipher.audit_logs (
                        id, correlation_id, audit_type, entity_type,
                        entity_id, payload, created_at
                    ) VALUES (
                        :id, :correlation_id, 'PAPER_RETENTION_ARCHIVE',
                        'maintenance', :entity_id, CAST(:payload AS jsonb), :created_at
                    )
                    """
                ),
                {
                    "id": audit_id,
                    "correlation_id": correlation_id,
                    "entity_id": correlation_id,
                    "payload": payload,
                    "created_at": datetime.now(timezone.utc),
                },
            )

    @staticmethod
    def _artifact_summary(artifact: ArchiveArtifact) -> dict[str, Any]:
        return {
            "kind": artifact.kind,
            "path": str(artifact.path),
            "sha256": artifact.sha256,
            "record_count": artifact.record_count,
            "stored_bytes": artifact.stored_bytes,
        }


async def run_from_env() -> dict[str, Any]:
    config = PaperRetentionConfig.from_env()
    engine = create_async_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        service = PaperRetentionService(engine, config)
        summary = await service.run()
        print("PAPER_RETENTION_COMPLETE", json.dumps(summary, sort_keys=True), flush=True)
        return summary
    finally:
        await engine.dispose()

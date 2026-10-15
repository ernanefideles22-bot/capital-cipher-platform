"""Bounded archive-before-prune retention for high-volume PAPER evidence.

This worker is intentionally separate from the trading runtime.  It reuses the
same fail-closed PAPER/staging configuration and durable archive format as the
base retention worker, but covers the high-write observational tables that can
otherwise exhaust the small staging PostgreSQL volume.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.maintenance.paper_retention import (
    PaperRetentionConfig,
    PaperRetentionService,
    _RETENTION_GUARD_VALUE,
)

GuardKind = Literal["specialist", "portfolio"]

_STRICT_SPECIALIST_GUARD = """
CREATE OR REPLACE FUNCTION capital_cipher.reject_specialist_evaluation_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    RAISE EXCEPTION 'specialist evaluation evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""

_RETENTION_SPECIALIST_GUARD = f"""
CREATE OR REPLACE FUNCTION capital_cipher.reject_specialist_evaluation_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting(
            'capital_cipher.paper_retention_authorized', true
       ) = '{_RETENTION_GUARD_VALUE}' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'specialist evaluation evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""

_STRICT_PORTFOLIO_GUARD = """
CREATE OR REPLACE FUNCTION capital_cipher.reject_portfolio_consensus_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    RAISE EXCEPTION 'portfolio and consensus evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""

_RETENTION_PORTFOLIO_GUARD = f"""
CREATE OR REPLACE FUNCTION capital_cipher.reject_portfolio_consensus_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $function$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting(
            'capital_cipher.paper_retention_authorized', true
       ) = '{_RETENTION_GUARD_VALUE}' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'portfolio and consensus evidence is append-only'
        USING ERRCODE = '55000';
END;
$function$
"""


@dataclass(frozen=True)
class HighVolumeTarget:
    archive_kind: str
    table_name: str
    id_column: str
    timestamp_column: str
    guard: GuardKind | None = None
    extra_predicate: str = ""


# Outcomes must be removed before forecasts because outcomes reference forecasts
# with ON DELETE RESTRICT.  Forecasts are additionally protected from deletion
# while any retained outcome still references them.
HIGH_VOLUME_TARGETS: tuple[HighVolumeTarget, ...] = (
    HighVolumeTarget(
        archive_kind="agent-forecast-outcomes",
        table_name="agent_forecast_outcomes",
        id_column="outcome_id",
        timestamp_column="created_at",
        guard="specialist",
    ),
    HighVolumeTarget(
        archive_kind="agent-forecasts",
        table_name="agent_forecasts",
        id_column="forecast_id",
        timestamp_column="created_at",
        guard="specialist",
        extra_predicate=(
            "AND NOT EXISTS ("
            "SELECT 1 FROM capital_cipher.agent_forecast_outcomes o "
            "WHERE o.forecast_id = t.forecast_id)"
        ),
    ),
    HighVolumeTarget(
        archive_kind="drift-observations",
        table_name="drift_observations",
        id_column="observation_id",
        timestamp_column="created_at",
        guard="portfolio",
    ),
    HighVolumeTarget(
        archive_kind="agent-outputs",
        table_name="agent_outputs",
        id_column="id",
        timestamp_column="created_at",
    ),
    HighVolumeTarget(
        archive_kind="raw-market-events",
        table_name="raw_market_events",
        id_column="event_id",
        timestamp_column="received_at",
    ),
)


class HighVolumePaperRetentionService:
    """Archive then prune high-volume PAPER tables in bounded batches."""

    def __init__(self, engine: AsyncEngine, config: PaperRetentionConfig) -> None:
        self._engine = engine
        self._config = config
        self._archive = PaperRetentionService(engine, config)

    async def run(self) -> dict[str, Any]:
        await self._verify_admin_connection()
        started_at = datetime.now(timezone.utc)
        cutoff = started_at - timedelta(hours=self._config.hot_hours)
        summary: dict[str, Any] = {
            "started_at": started_at.isoformat(),
            "cutoff": cutoff.isoformat(),
            "hot_hours": self._config.hot_hours,
            "archive_days": self._config.archive_days,
            "rows_archived": {},
            "artifacts": [],
        }

        for target in HIGH_VOLUME_TARGETS:
            archived = 0
            for _ in range(self._config.max_batches_per_kind):
                artifact = await self._archive_and_prune_target(target, cutoff)
                if artifact is None:
                    break
                archived += artifact.record_count
                summary["artifacts"].append(self._archive._artifact_summary(artifact))
                if artifact.record_count < self._config.event_batch_size:
                    break
            summary["rows_archived"][target.table_name] = archived

        summary["archive_files_pruned"] = self._archive._prune_expired_archives(
            started_at
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
        target_tables = ",".join(
            f"'capital_cipher.{target.table_name}'" for target in HIGH_VOLUME_TARGETS
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"""
                        SELECT table_name,
                               has_table_privilege(
                                   current_user,
                                   'capital_cipher.' || table_name,
                                   'DELETE'
                               ) AS can_delete
                        FROM information_schema.tables
                        WHERE table_schema = 'capital_cipher'
                          AND table_name IN ({target_tables})
                        ORDER BY table_name
                        """
                    )
                )
            ).mappings().all()
            current_user = await conn.scalar(text("SELECT current_user"))
        if current_user == "capital_cipher_app":
            raise RuntimeError("High-volume PAPER retention refuses application login")
        seen = {str(row["table_name"]): bool(row["can_delete"]) for row in rows}
        expected = {target.table_name for target in HIGH_VOLUME_TARGETS}
        if set(seen) != expected or not all(seen.values()):
            raise RuntimeError(
                "High-volume PAPER retention requires DELETE on every target table"
            )

    async def _archive_and_prune_target(
        self,
        target: HighVolumeTarget,
        cutoff: datetime,
    ):
        extra = f" {target.extra_predicate}" if target.extra_predicate else ""
        select_sql = text(
            f"""
            SELECT t.{target.id_column} AS row_id, to_jsonb(t) AS document
            FROM capital_cipher.{target.table_name} t
            WHERE t.{target.timestamp_column} < :cutoff
            {extra}
            ORDER BY t.{target.timestamp_column}, t.{target.id_column}
            LIMIT :batch_size
            """
        )
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select_sql,
                    {
                        "cutoff": cutoff,
                        "batch_size": self._config.event_batch_size,
                    },
                )
            ).mappings().all()
        if not rows:
            return None

        documents = [row["document"] for row in rows]
        ids = [str(row["row_id"]) for row in rows]
        artifact = self._archive._write_archive(
            target.archive_kind,
            documents,
            cutoff,
        )

        delete_extra = f" {target.extra_predicate}" if target.extra_predicate else ""
        async with self._engine.begin() as conn:
            if target.guard is not None:
                await self._enable_retention_guard(conn, target.guard)
            await conn.execute(
                text(
                    "SET LOCAL capital_cipher.paper_retention_authorized = "
                    f"'{_RETENTION_GUARD_VALUE}'"
                )
            )
            deleted = await conn.execute(
                text(
                    f"""
                    DELETE FROM capital_cipher.{target.table_name} t
                    WHERE t.{target.id_column} = ANY(CAST(:ids AS varchar[]))
                      AND t.{target.timestamp_column} < :cutoff
                    {delete_extra}
                    """
                ),
                {"ids": ids, "cutoff": cutoff},
            )
            if deleted.rowcount != len(ids):
                raise RuntimeError(
                    f"PAPER retention delete count changed for {target.table_name}"
                )
            if target.guard is not None:
                await self._restore_strict_guard(conn, target.guard)

        print(
            "PAPER_RETENTION_HIGH_VOLUME",
            json.dumps(
                {
                    "table": target.table_name,
                    "rows": deleted.rowcount,
                    "artifact": self._archive._artifact_summary(artifact),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return artifact

    @staticmethod
    async def _enable_retention_guard(conn, guard: GuardKind) -> None:
        statement = (
            _RETENTION_SPECIALIST_GUARD
            if guard == "specialist"
            else _RETENTION_PORTFOLIO_GUARD
        )
        await conn.execute(text(statement))

    @staticmethod
    async def _restore_strict_guard(conn, guard: GuardKind) -> None:
        statement = (
            _STRICT_SPECIALIST_GUARD
            if guard == "specialist"
            else _STRICT_PORTFOLIO_GUARD
        )
        await conn.execute(text(statement))

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
                        :id, :correlation_id, 'PAPER_RETENTION_HIGH_VOLUME',
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


async def run_high_volume_from_env() -> dict[str, Any]:
    config = PaperRetentionConfig.from_env()
    engine = create_async_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
    )
    try:
        service = HighVolumePaperRetentionService(engine, config)
        summary = await service.run()
        print(
            "PAPER_RETENTION_HIGH_VOLUME_COMPLETE",
            json.dumps(summary, sort_keys=True),
            flush=True,
        )
        return summary
    finally:
        await engine.dispose()

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import Text

from app.database.models import AuditLogModel, Base, INTERNAL_SCHEMA
from app.database.session import Database


BOOTSTRAP_TABLES = {
    "agent_outputs",
    "audit_logs",
    "backfill_queue_items",
    "backfill_raw_pages",
    "candle_observations",
    "clock_observations",
    "dataset_manifests",
    "decisions",
    "event_journal",
    "event_outbox",
    "historical_backfill_jobs",
    "market_candles",
    "market_data_gaps",
    "paper_orders",
    "raw_data_objects",
    "raw_market_events",
    "replay_checkpoints",
    "risk_checks",
    "system_events",
}


def test_every_application_model_uses_the_private_schema() -> None:
    schemas = {table.schema for table in Base.metadata.sorted_tables}

    assert schemas == {INTERNAL_SCHEMA}


@pytest.mark.asyncio
async def test_schema_verification_never_creates_missing_tables() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")

    try:
        with pytest.raises(RuntimeError, match="missing tables"):
            await database.verify_schema()

        await database.create_all()
        await database.verify_schema()
    finally:
        await database.dispose()


def test_bootstrap_migration_is_private_and_least_privilege() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    migration = (
        repository_root
        / "supabase"
        / "migrations"
        / "20260723001208_bootstrap_private_schema_and_runtime_role.sql"
    ).read_text(encoding="utf-8")

    created_tables = set(
        re.findall(r"CREATE TABLE capital_cipher\.([a-z0-9_]+)", migration)
    )

    assert created_tables == BOOTSTRAP_TABLES
    assert "CREATE TABLE public." not in migration
    assert "create role capital_cipher_runtime" in migration
    assert "nologin nosuperuser" in migration
    assert "nobypassrls" in migration
    assert "alter role capital_cipher_runtime" not in migration
    assert "capital_cipher_runtime must be a NOLOGIN least-privilege role" in migration
    assert "grant select, insert on all tables" in migration
    assert "revoke delete, truncate, references, trigger" in migration
    assert "for select to capital_cipher_runtime" in migration
    assert "for insert to capital_cipher_runtime" in migration
    assert "for update to capital_cipher_runtime" in migration


def test_staging_startup_verifies_instead_of_creating_schema() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    main_source = (repository_root / "backend" / "app" / "main.py").read_text(
        encoding="utf-8"
    )

    staging_branch = (
        'if settings.app_env == "staging":\n'
        "                await ctx.database.verify_schema()"
    )

    assert staging_branch in main_source


def test_audit_entity_ids_preserve_content_addressed_identifiers() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    migration = (
        repository_root
        / "supabase"
        / "migrations"
        / "20260723063511_expand_audit_entity_id.sql"
    ).read_text(encoding="utf-8").lower()

    assert isinstance(AuditLogModel.__table__.c.entity_id.type, Text)
    assert "alter table capital_cipher.audit_logs" in migration
    assert "alter column entity_id type text" in migration

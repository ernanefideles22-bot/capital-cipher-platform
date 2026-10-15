"""Safety and archive-integrity tests for PAPER retention."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.maintenance.paper_retention import (
    PaperRetentionConfig,
    PaperRetentionService,
)


def _config(tmp_path: Path) -> PaperRetentionConfig:
    return PaperRetentionConfig(
        database_url="postgresql+asyncpg://postgres:test@postgreseu2.railway.internal:5432/railway",
        archive_root=tmp_path / "archive",
        expected_database_host="postgreseu2.railway.internal",
    )


def test_retention_archive_is_deterministic_and_hash_verified(tmp_path):
    config = _config(tmp_path)
    service = PaperRetentionService(cast(AsyncEngine, None), config)
    cutoff = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    records = [
        {"execution_id": "exec-1", "payload": {"signal": "WAIT", "score": 0.5}},
        {"execution_id": "exec-2", "payload": {"signal": "LONG", "score": 0.7}},
    ]

    first = service._write_archive("agent-runtime", records, cutoff)
    second = service._write_archive("agent-runtime", records, cutoff)

    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.record_count == 2
    stored = first.path.read_bytes()
    uncompressed = gzip.decompress(stored)
    assert hashlib.sha256(uncompressed).hexdigest() == first.sha256
    lines = uncompressed.decode("utf-8").splitlines()
    header = json.loads(lines[0])
    assert header == {
        "archive_schema_version": "1.0",
        "cutoff": "2026-10-15T12:00:00+00:00",
        "kind": "agent-runtime",
        "record_count": 2,
    }
    assert json.loads(lines[1]) == records[0]
    assert json.loads(lines[2]) == records[1]


def test_retention_config_requires_paper_staging(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_RETENTION_ENABLED", "true")
    monkeypatch.setenv("SYSTEM_MODE", "PAPER")
    monkeypatch.setenv("OMS_EXECUTION_ENVIRONMENT", "PAPER")
    monkeypatch.setenv("OMS_TESTNET_ENABLED", "false")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql://postgres:test@postgreseu2.railway.internal:5432/railway",
    )
    monkeypatch.setenv("PAPER_RETENTION_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(
        "PAPER_RETENTION_EXPECTED_DATABASE_HOST",
        "postgreseu2.railway.internal",
    )

    config = PaperRetentionConfig.from_env()

    assert config.hot_hours == 24
    assert config.archive_days == 7
    assert config.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SYSTEM_MODE", "LIVE"),
        ("OMS_EXECUTION_ENVIRONMENT", "TESTNET"),
        ("OMS_TESTNET_ENABLED", "true"),
        ("RAILWAY_ENVIRONMENT_NAME", "production"),
    ],
)
def test_retention_config_fails_closed_outside_paper_staging(
    monkeypatch,
    tmp_path,
    name,
    value,
):
    monkeypatch.setenv("PAPER_RETENTION_ENABLED", "true")
    monkeypatch.setenv("SYSTEM_MODE", "PAPER")
    monkeypatch.setenv("OMS_EXECUTION_ENVIRONMENT", "PAPER")
    monkeypatch.setenv("OMS_TESTNET_ENABLED", "false")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql://postgres:test@postgreseu2.railway.internal:5432/railway",
    )
    monkeypatch.setenv("PAPER_RETENTION_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError):
        PaperRetentionConfig.from_env()


def test_retention_config_rejects_wrong_database_host(tmp_path):
    with pytest.raises(ValueError):
        PaperRetentionConfig(
            database_url="postgresql+asyncpg://postgres:test@old-db.railway.internal:5432/railway",
            archive_root=tmp_path / "archive",
            expected_database_host="postgreseu2.railway.internal",
        ).validate()

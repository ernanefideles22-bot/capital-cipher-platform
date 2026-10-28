"""Prove the final hosted harness reaches its faults and cannot target main data."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest

from app.core.config import Settings
from app.validation.office_runtime_certification import certify_cpu_isolation, certify_dependency_fault

SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
hosted = importlib.import_module("certify_railway_office_runtime")


def isolated_settings(tmp_path):
    return Settings(_env_file=None, app_env="test", office_async_enabled=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'certification.db'}",
        redis_url=None, event_broker_required=False, enable_market_data=False,
        agent_worker_max_concurrency=2, agent_worker_batch_size=1,
        operations_monitor_enabled=False, backfill_worker_enabled=False,
        data_lake_root=str(tmp_path / "lake"), log_level="ERROR")


@pytest.mark.parametrize("timeframe", ["15m", "1h", "4h"])
async def test_hosted_cpu_probe_reaches_loop_and_preserves_real_paper_order(tmp_path, timeframe):
    result = await certify_cpu_isolation(isolated_settings(tmp_path), timeframe, tmp_path / "markers")
    assert result["cpu_loop_started"] and result["cpu_child_terminated"]
    assert result["paper_order_completed_while_cpu_loop_running"]
    assert result["rebuild_preserved_closed_order"] and result["duplicate_rejected"]


@pytest.mark.parametrize("fault", ["database", "broker", "audit"])
async def test_hosted_fault_probe_reaches_dependency_and_blocks_order(tmp_path, fault):
    result = await certify_dependency_fault(isolated_settings(tmp_path), fault)
    assert result["boundary_reached"] and result["fail_closed"]
    assert result["new_paper_orders"] == 0


@pytest.mark.parametrize("name", ["capital_cipher_staging", "postgres",
    "capital_cipher_final_b_" + "a" * 12 + "_flow;drop database postgres",
    "capital_cipher_paper_validation_" + "a" * 12 + "_flow"])
def test_final_harness_rejects_main_previous_and_arbitrary_database_names(name):
    with pytest.raises(RuntimeError, match="CERTIFICATION_DATABASE_NAME_REFUSED"):
        hosted.isolated_url("postgresql://unused@capital-cipher-recovered.railway.internal/capital_cipher_staging", name)


def test_final_harness_refuses_wrong_region_before_connecting(monkeypatch):
    monkeypatch.setenv("PHASE_B_FINAL_CERTIFICATION_ENABLED", "true")
    monkeypatch.setenv("APP_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("PAPER_VALIDATION_EXPECTED_REGION", "europe-west4-drams3a")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", "sfo")
    with pytest.raises(RuntimeError, match="PAPER_VALIDATION_REGION_MISMATCH"):
        hosted.require_scope()


async def test_final_harness_storage_limit_preserves_evidence():
    class LargeDatabases:
        async def fetchval(self, query, name):
            return hosted.MAX_TOTAL_DATABASE_BYTES + 1
    with pytest.raises(RuntimeError, match="CERTIFICATION_RETAINED_STORAGE_BUDGET_EXCEEDED"):
        await hosted.retained_sizes(LargeDatabases(), {"flow": "existing-copy"})


def test_final_harness_refuses_relaxed_primary_deadline(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused@capital-cipher-recovered.railway.internal/capital_cipher_staging")
    monkeypatch.setenv("OFFICE_PRIMARY_TIMEOUT_SECONDS", "4")
    with pytest.raises(RuntimeError, match="CERTIFICATION_REVIEWED_DEADLINES_REQUIRED"):
        hosted.isolated_settings(hosted.PREFIX + "a" * 12 + "_flow", "a" * 40)

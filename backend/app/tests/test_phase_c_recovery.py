"""Actual process death at PAPER transaction boundaries and concurrent faults."""
import os
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from app.tests.test_office_async import settings
from app.tests.test_phase_c_pipeline import new_pg17_database
from app.validation.phase_c_runtime import (
    CRASH_BOUNDARIES, validate_capacity_conflict, validate_concurrent_fault,
    validate_crash_recovery, validate_locked_expiry, validate_three_desks,
)


@pytest.mark.parametrize("boundary", CRASH_BOUNDARIES)
async def test_process_death_rebuild_and_replay(tmp_path, boundary):
    result = await validate_crash_recovery(settings(tmp_path,
        max_single_position_percent=20, max_strategy_exposure_percent=20), boundary, tmp_path)
    assert result["process_exit"] == 77
    assert result["replay_no_duplicate"]


@pytest.mark.parametrize("fault", ["database", "broker", "audit"])
async def test_three_desks_fail_closed_and_rebuild(tmp_path, fault):
    assert (await validate_concurrent_fault(settings(tmp_path), fault))["orders"] == 0


async def test_hosted_probe_three_desks_fixture(tmp_path):
    result = await validate_three_desks(settings(tmp_path,
        max_single_position_percent=20, max_strategy_exposure_percent=20))
    assert result["fills"] == 3


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
@pytest.mark.parametrize("boundary", CRASH_BOUNDARIES)
async def test_pg17_real_process_death_recovery(tmp_path, boundary):
    target = await new_pg17_database()
    redis = os.environ.get("REDIS_TEST_URL")
    if redis:
        assert urlsplit(redis).hostname in {"localhost", "127.0.0.1", "::1"}
    config = settings(tmp_path, app_env="local",
        database_url=target.replace("postgresql://", "postgresql+asyncpg://"),
        redis_url=redis, event_broker_required=bool(redis), redis_stream_prefix="cc-crash-"+uuid4().hex,
        max_single_position_percent=20, max_strategy_exposure_percent=20)
    assert (await validate_crash_recovery(config, boundary, tmp_path))["process_exit"] == 77


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
@pytest.mark.parametrize("opposing", [False, True])
async def test_pg17_hosted_capacity_probe(tmp_path, opposing):
    target = await new_pg17_database()
    config = settings(tmp_path, database_url=target.replace("postgresql://", "postgresql+asyncpg://"))
    assert (await validate_capacity_conflict(config, opposing))["accepted"] == 1


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_pg17_hosted_expiry_probe(tmp_path):
    target = await new_pg17_database()
    config = settings(tmp_path, database_url=target.replace("postgresql://", "postgresql+asyncpg://"))
    assert (await validate_locked_expiry(config))["expired_rejected"]

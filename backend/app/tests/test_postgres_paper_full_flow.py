"""Native PG17/Redis PAPER integration in a new local CI database only."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
@pytest.mark.parametrize("office_enabled", [False, True])
async def test_native_postgres17_redis_paper_full_flow(office_enabled):
    import asyncpg

    url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    assert url.hostname in {"localhost", "127.0.0.1", "::1"}, "CI fixture refuses hosted databases"
    name = "capital_cipher_paper_validation_" + uuid4().hex[:12] + "_flow"
    admin = await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await admin.fetchval("show server_version_num")) // 10000 == 17
        await admin.execute(f'create database "{name}" template template0')
    finally:
        await admin.close()
    target_url = urlunsplit(url._replace(path="/" + name))
    connection = await asyncpg.connect(target_url)
    try:
        migrations = sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql"))
        for migration in migrations:
            async with connection.transaction():
                await connection.execute(migration.read_text())
    finally:
        await connection.close()
    settings = Settings(
        OFFICE_ASYNC_ENABLED=office_enabled,
        APP_ENV="local", DATABASE_URL=target_url.replace("postgresql://", "postgresql+asyncpg://"),
        REDIS_URL=os.environ.get("REDIS_TEST_URL"),
        REDIS_STREAM_PREFIX="cc-native-paper-" + uuid4().hex[:12],
        EVENT_BROKER_REQUIRED=bool(os.environ.get("REDIS_TEST_URL")),
        AGENT_WORKER_ENABLED=True, BACKFILL_WORKER_ENABLED=False,
        ENABLE_MARKET_DATA=False, OPERATIONS_MONITOR_ENABLED=False,
        OMS_WORKER_ENABLED=False, OMS_RECONCILIATION_ENABLED=False,
        ADMIN_API_KEY="isolated-native-paper-ci-administration-key",
        DATA_LAKE_ROOT="/tmp/cc-native-paper-" + name, LOG_LEVEL="ERROR",
    )
    results = [await validate_full_paper_flow(settings, direction, side)
               for direction, side in ((1, "BUY"), (-1, "SELL"))]
    assert all(result["completed_agent_jobs"] == 300 for result in results)
    safety = await validate_paper_safety(settings)
    print(json.dumps({"native_postgres_version": 17, "flows": results, "safety": safety}))

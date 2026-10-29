"""Native PG17/Redis PAPER integration in a new local CI database only."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
@pytest.mark.parametrize("office_enabled", [False, True])
@pytest.mark.parametrize("timeframe", ["15m", "1h", "4h"])
async def test_native_postgres17_redis_paper_full_flow(office_enabled, timeframe):
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
    results = [await validate_full_paper_flow(settings, direction, side, timeframe=timeframe)
               for direction, side in ((1, "BUY"), (-1, "SELL"))]
    assert all(result["completed_agent_jobs"] == 300 for result in results)
    safety = await validate_paper_safety(settings)
    print(json.dumps({"native_postgres_version": 17, "flows": results, "safety": safety}))


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_postgres_trace_is_one_snapshot_during_worker_commit(monkeypatch):
    import asyncpg
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy import text
    from app.agents.registry import AgentRegistry
    from app.agents.runtime import AgentRuntime
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    from app.tests.test_month_5_agent_runtime import SuccessfulAgent, _request

    url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    assert url.hostname in {"localhost", "127.0.0.1", "::1"}
    name = "capital_cipher_paper_validation_" + uuid4().hex[:12] + "_flow"
    control = await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await control.fetchval("show server_version_num")) // 10000 == 17
        await control.execute(f'create database "{name}" template template0')
    finally:
        await control.close()
    target = urlunsplit(url._replace(path="/" + name))
    connection = await asyncpg.connect(target)
    try:
        for migration in sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql")):
            await connection.execute(migration.read_text())
    finally:
        await connection.close()
    database = Database(target.replace("postgresql://", "postgresql+asyncpg://"), pool_size=2)
    repository = Repository(database)
    agent = SuccessfulAgent()
    runtime = AgentRuntime(AgentRegistry([agent]), repository=repository)
    await runtime.initialize()
    request = _request(agent)
    job = await runtime.submit(request)
    real_scalars = AsyncSession.scalars
    paused = False
    reader_task = None

    async def concurrent_finish(session, statement, *args, **kwargs):
        nonlocal paused
        result = await real_scalars(session, statement, *args, **kwargs)
        if (asyncio.current_task() is reader_task and not paused
                and "agent_execution_jobs" in str(statement)):
            paused = True
            # Force a real commit after the job SELECT and before attempts and
            # memory SELECTs. A READ COMMITTED reader gets a contradictory trace.
            completed = await runtime.execute(request)
            assert completed.job.status == "COMPLETED"
        return result

    monkeypatch.setattr(AsyncSession, "scalars", concurrent_finish)
    try:
        reader_task = asyncio.create_task(repository.load_agent_execution_trace(job.execution_id))
        before = await asyncio.wait_for(reader_task, 10)
        assert paused and before.job.status == "PENDING"
        assert before.job.attempt_count == 0 and not before.attempts and len(before.memory) == 1
        after = await repository.load_agent_execution_trace(job.execution_id)
        assert after.job.status == "COMPLETED" and len(after.attempts) == 1 and len(after.memory) == 3
        async with database.session() as session:
            conn = await session.connection()
            assert await conn.get_isolation_level() == "READ COMMITTED"
            assert await conn.scalar(text("show transaction_read_only")) == "off"
    finally:
        await database.dispose()


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_ingest_lag_widening_preserves_history_security_and_large_backfill():
    import asyncpg
    from datetime import datetime, timedelta, timezone
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    from app.schemas.common import Exchange
    from app.schemas.market import Candle

    url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    assert url.hostname in {"localhost", "127.0.0.1", "::1"}, "Migration test refuses hosted databases"
    name = "capital_cipher_lag_validation_" + uuid4().hex[:12]
    admin = await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await admin.fetchval("show server_version_num")) // 10000 == 17
        await admin.execute(f'create database "{name}" template template0')
    finally:
        await admin.close()
    target = urlunsplit(url._replace(path="/" + name))
    connection = await asyncpg.connect(target)
    database = None
    try:
        migrations = sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql"))
        target_migration = next(m for m in migrations if m.name == "20261027002000_widen_candle_ingest_lag.sql")
        for migration in (m for m in migrations if m.name < target_migration.name):
            async with connection.transaction():
                await connection.execute(migration.read_text())
        now = datetime.now(timezone.utc)
        for index, lag in enumerate((0, 2**31 - 1)):
            await connection.execute("""insert into capital_cipher.candle_observations
                (candle_id,schema_version,exchange,symbol,timeframe,open,high,low,close,volume,
                 closed_at,received_at,ingest_lag_ms,quality_score,quality_status,quality_warnings,
                 quality_errors,recorded_at)
                values ($1,'1.0.0','BINANCE',$2,'4h',100,101,99,100,100,$3,$4,$5,NULL,
                        'UNASSESSED','[]','[]',$4)""",
                str(index) * 64, ("BTCUSDT", "ETHUSDT")[index], now - timedelta(milliseconds=lag), now, lag)
        rows_sql = "select jsonb_agg(to_jsonb(c) order by candle_id)::text from capital_cipher.candle_observations c"
        security_sql = """select relowner,relrowsecurity,relforcerowsecurity,relacl::text
            from pg_class where oid='capital_cipher.candle_observations'::regclass"""
        constraints_sql = """select conname,pg_get_constraintdef(oid),convalidated from pg_constraint
            where conrelid='capital_cipher.candle_observations'::regclass order by conname"""
        before = await connection.fetchval(rows_sql)
        security = await connection.fetchrow(security_sql)
        constraints = await connection.fetch(constraints_sql)
        previous_timeouts = (await connection.fetchval("show lock_timeout"), await connection.fetchval("show statement_timeout"))
        async with connection.transaction():
            await connection.execute(target_migration.read_text())
        assert await connection.fetchval(rows_sql) == before
        assert await connection.fetchrow(security_sql) == security
        assert await connection.fetch(constraints_sql) == constraints
        assert await connection.fetchval("""select format_type(atttypid,atttypmod) from pg_attribute
            where attrelid='capital_cipher.candle_observations'::regclass and attname='ingest_lag_ms'""") == "bigint"
        assert previous_timeouts == (await connection.fetchval("show lock_timeout"), await connection.fetchval("show statement_timeout"))
        database = Database(target.replace("postgresql://", "postgresql+asyncpg://"), pool_size=2)
        candle = Candle(exchange=Exchange.BINANCE, symbol="SOLUSDT", timeframe="4h", open=100,
            high=101, low=99, close=100, volume=100, closed_at=now-timedelta(days=36), received_at=now)
        await Repository(database).save_candle(candle)
        assert await connection.fetchval("select ingest_lag_ms from capital_cipher.candle_observations where symbol='SOLUSDT'") == 3_110_400_000
        after = await connection.fetchval(rows_sql)
        async with connection.transaction():
            await connection.execute(target_migration.read_text())
        assert await connection.fetchval(rows_sql) == after
    finally:
        if database is not None:
            await database.dispose()
        await connection.close()

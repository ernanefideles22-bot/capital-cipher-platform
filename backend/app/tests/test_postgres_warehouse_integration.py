"""Real PostgreSQL warehouse integration test, enabled by POSTGRES_TEST_URL."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import SQLAlchemyError

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.agents.specialists import MomentumAgent
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import WalkForwardEngine
from app.database.models import (
    AgentExecutionAttemptModel,
    INTERNAL_SCHEMA,
    WalkForwardExperimentModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.catalog import DataCatalog
from app.market_data.gaps import GapService
from app.market_data.store import CandleStore
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.backfill import HistoricalBackfillJob
from app.schemas.backtest import WalkForwardProtocol, WalkForwardRequest
from app.schemas.data_lake import (
    BackfillQueueItem,
    BackfillRawPageLink,
    RawDataObject,
)
from app.tests.conftest import make_series


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="POSTGRES_TEST_URL is not configured",
)
async def test_real_postgres_internal_warehouse_round_trip():
    import asyncpg

    migration_root = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
    )
    migration_connection = await asyncpg.connect(
        os.environ["POSTGRES_TEST_URL"].replace(
            "postgresql+asyncpg://",
            "postgresql://",
        )
    )
    try:
        for migration_path in sorted(migration_root.glob("*.sql")):
            await migration_connection.execute(
                migration_path.read_text(encoding="utf-8")
            )
    finally:
        await migration_connection.close()

    database = Database(os.environ["POSTGRES_TEST_URL"])
    await database.create_all()
    repository = Repository(database)
    candles = make_series([100.0, 101.0, 102.0])

    assert await repository.save_candles(candles) == 3
    manifest = await DataCatalog(repository).materialize_candle_dataset(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        clock_status="SYNCED",
    )
    loaded = await repository.load_dataset_manifest(manifest.dataset_hash)
    gaps = await GapService(repository).scan(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=candles[0].closed_at,
        end_at=candles[-1].closed_at,
    )
    jobs = []
    for identity in ("d" * 64, "e" * 64):
        job = HistoricalBackfillJob(
            job_id=identity,
            request_fingerprint=identity,
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            start_at=candles[0].closed_at,
            end_at=candles[-1].closed_at,
            source="binance.public-rest",
        )
        await repository.submit_historical_backfill(
            job,
            BackfillQueueItem(
                queue_id=identity,
                job_id=identity,
                exchange="BINANCE",
                symbol="BTCUSDT",
                timeframe="15m",
                start_at=candles[0].closed_at,
                end_at=candles[-1].closed_at,
                max_candles=3,
            ),
        )
        jobs.append(job)
    loaded_job = await repository.load_historical_backfill_job(jobs[0].job_id)
    claims = await asyncio.gather(
        repository.claim_next_backfill(
            worker_id="postgres-worker-one",
            lease_seconds=60,
        ),
        repository.claim_next_backfill(
            worker_id="postgres-worker-two",
            lease_seconds=60,
        ),
    )
    raw_object = RawDataObject(
        object_hash="f" * 64,
        object_uri=(
            "lake://raw/binance.public-rest/2026/07/20/ff/"
            f"{'f' * 64}.json.gz"
        ),
        uncompressed_bytes=100,
        stored_bytes=80,
    )
    raw_link = BackfillRawPageLink(
        page_id="a" * 64,
        job_id=jobs[0].job_id,
        attempt_count=1,
        page_index=0,
        object_hash=raw_object.object_hash,
        source="binance.public-rest",
        endpoint="/api/v3/klines",
        request_params={"symbol": "BTCUSDT"},
        fetched_at=candles[-1].received_at,
    )
    await repository.save_backfill_raw_page(raw_object, raw_link)
    loaded_raw_pages = await repository.list_backfill_raw_pages(jobs[0].job_id)
    artifact_candles = make_series(
        [100.0 + index * 0.1 for index in range(30)]
    )
    artifact = await WalkForwardEngine(BacktestingEngine()).run(
        WalkForwardRequest(
            candidate_version="SCALP_15M_v1",
            protocol=WalkForwardProtocol(
                train_candles=10,
                validation_candles=10,
                test_candles=10,
                embargo_candles=0,
                max_folds=1,
            ),
        ),
        artifact_candles,
    )
    await repository.save_walk_forward_report(artifact)
    loaded_artifact = await repository.load_walk_forward_report(
        artifact.experiment_id
    )
    momentum_agent = MomentumAgent(CandleStore())
    runtime = AgentRuntime(
        AgentRegistry([momentum_agent]),
        repository=repository,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )
    runtime_trace = await runtime.execute(
        AgentExecutionRequest(
            idempotency_key="postgres-agent-runtime",
            input=AgentInput(
                request_id="postgres-agent-runtime",
                correlation_id="postgres-agent-correlation",
                agent_name=momentum_agent.name,
                symbol="BTCUSDT",
                timeframe="15m",
            ),
        )
    )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(WalkForwardExperimentModel)
                .where(
                    WalkForwardExperimentModel.experiment_id
                    == artifact.experiment_id
                )
                .values(symbol="ETHUSDT")
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(AgentExecutionAttemptModel)
                .where(
                    AgentExecutionAttemptModel.execution_id
                    == runtime_trace.job.execution_id
                )
                .values(worker_id="tampered-worker")
            )

    async with database.engine.connect() as connection:
        tables = set(
            await connection.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema_name"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        immutable_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table = "
                    "'walk_forward_experiments'"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        row_security_enabled = await connection.scalar(
            text(
                "SELECT c.relrowsecurity "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname = 'walk_forward_experiments'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        function_is_security_definer = await connection.scalar(
            text(
                "SELECT p.prosecdef "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname = "
                "'reject_walk_forward_experiment_mutation'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        agent_immutable_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table IN "
                    "('agent_execution_attempts', 'agent_memory_entries')"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        agent_rls_count = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname IN "
                "('agent_execution_jobs', 'agent_execution_attempts', "
                "'agent_memory_entries') "
                "AND c.relrowsecurity"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        agent_function_is_security_definer = await connection.scalar(
            text(
                "SELECT p.prosecdef "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname = 'reject_agent_evidence_mutation'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
    await database.dispose()

    assert loaded == manifest
    assert gaps == []
    assert loaded_job == jobs[0]
    assert loaded_artifact == artifact
    assert runtime_trace.job.status == "COMPLETED"
    assert len(runtime_trace.attempts) == 1
    assert len(runtime_trace.memory) == 3
    assert {claim.queue_id for claim in claims if claim is not None} == {
        jobs[0].job_id,
        jobs[1].job_id,
    }
    assert loaded_raw_pages == [raw_link]
    assert {
        "candle_observations",
        "dataset_manifests",
        "clock_observations",
        "market_data_gaps",
        "historical_backfill_jobs",
        "backfill_queue_items",
        "raw_data_objects",
        "backfill_raw_pages",
        "walk_forward_experiments",
        "agent_execution_jobs",
        "agent_execution_attempts",
        "agent_memory_entries",
    } <= tables
    assert "trg_walk_forward_experiments_immutable" in immutable_triggers
    assert row_security_enabled is True
    assert function_is_security_definer is False
    assert agent_immutable_triggers == {
        "trg_agent_execution_attempts_immutable",
        "trg_agent_memory_entries_immutable",
    }
    assert agent_rls_count == 3
    assert agent_function_is_security_definer is False

"""Real PostgreSQL warehouse integration test, enabled by POSTGRES_TEST_URL."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.database.models import INTERNAL_SCHEMA
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.catalog import DataCatalog
from app.market_data.gaps import GapService
from app.schemas.backfill import HistoricalBackfillJob
from app.tests.conftest import make_series


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="POSTGRES_TEST_URL is not configured",
)
async def test_real_postgres_internal_warehouse_round_trip():
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
    job = HistoricalBackfillJob(
        job_id="d" * 64,
        request_fingerprint="d" * 64,
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=candles[0].closed_at,
        end_at=candles[-1].closed_at,
        source="binance.public-rest",
    )
    await repository.save_historical_backfill_job(job)
    loaded_job = await repository.load_historical_backfill_job(job.job_id)

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
    await database.dispose()

    assert loaded == manifest
    assert gaps == []
    assert loaded_job == job
    assert {
        "candle_observations",
        "dataset_manifests",
        "clock_observations",
        "market_data_gaps",
        "historical_backfill_jobs",
    } <= tables

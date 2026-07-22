"""Time-series storage, catalog identity, and clock-quality tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import DataQualityError, DatabaseError
from app.database.models import (
    CandleObservationModel,
    ClockObservationModel,
    DatasetManifestModel,
    INTERNAL_SCHEMA,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.catalog import DataCatalog, build_candle_dataset_manifest
from app.market_data.clock import evaluate_clock_probe
from app.market_data.data_quality import evaluate_candles
from app.market_data.identity import candle_dataset_hash, candle_event_id
from app.market_data.store import CandleStore
from app.main import create_app
from app.tests.conftest import make_candle, make_series


def test_candle_identity_excludes_ingestion_time_but_not_market_facts():
    closed_at = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    first = make_candle(
        100.0,
        closed_at=closed_at,
    ).model_copy(update={"received_at": closed_at + timedelta(seconds=1)})
    later_ingestion = first.model_copy(
        update={"received_at": closed_at + timedelta(minutes=3)}
    )
    changed_close = first.model_copy(update={"close": 100.5, "high": 101.0})

    assert candle_event_id(first) == candle_event_id(later_ingestion)
    assert candle_dataset_hash([first]) == candle_dataset_hash([later_ingestion])
    assert candle_event_id(first) != candle_event_id(changed_close)


def test_store_rejects_out_of_order_and_ignores_exact_duplicate():
    store = CandleStore()
    latest = make_candle(
        closed_at=datetime(2026, 7, 1, 12, 15, tzinfo=timezone.utc)
    )
    earlier = make_candle(
        closed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    )

    assert store.add(latest) is True
    assert store.add(latest) is False
    with pytest.raises(DataQualityError, match="Out-of-order"):
        store.add(earlier)


async def test_time_series_batch_is_ordered_idempotent_and_quality_annotated():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    candles = make_series([100.0, 101.0, 102.0])
    quality = evaluate_candles(
        candles,
        timeframe="15m",
        check_staleness=False,
    )

    inserted = await repository.save_candles(
        candles,
        quality_reports=[quality] * len(candles),
    )
    repeated = await repository.save_candles(
        list(reversed(candles)),
        quality_reports=[quality] * len(candles),
    )
    loaded = await repository.list_candles(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
    )
    async with database.session() as session:
        rows = list(
            await session.scalars(
                select(CandleObservationModel).order_by(
                    CandleObservationModel.closed_at
                )
            )
        )
    await database.dispose()

    assert inserted == 3
    assert repeated == 0
    assert [candle.closed_at for candle in loaded] == sorted(
        candle.closed_at for candle in loaded
    )
    assert rows[0].quality_status == quality.status
    assert rows[0].quality_score == quality.data_quality_score


async def test_conflicting_candle_correction_fails_closed():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    candle = make_candle()
    corrected = candle.model_copy(
        update={"close": candle.close + 1, "high": candle.high + 2}
    )

    assert await repository.save_candle(candle) is True
    with pytest.raises(DatabaseError):
        await repository.save_candle(corrected)
    await database.dispose()


async def test_catalog_materializes_and_persists_stable_manifest():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    candles = make_series([100.0, 101.0, 102.0, 103.0])
    await repository.save_candles(candles)

    catalog = DataCatalog(repository)
    manifest = await catalog.materialize_candle_dataset(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        clock_status="SYNCED",
    )
    loaded = await repository.load_dataset_manifest(manifest.dataset_hash)
    repeated = await repository.save_dataset_manifest(manifest)
    await database.dispose()

    assert loaded == manifest
    assert manifest.row_count == 4
    assert manifest.dataset_id == f"candles:v1:{manifest.dataset_hash}"
    assert repeated is False


def test_manifest_rejects_empty_mixed_or_duplicate_series():
    with pytest.raises(DataQualityError, match="empty"):
        build_candle_dataset_manifest([])

    candles = make_series([100.0, 101.0])
    mixed = [candles[0], candles[1].model_copy(update={"symbol": "ETHUSDT"})]
    with pytest.raises(DataQualityError, match="exactly one series"):
        build_candle_dataset_manifest(mixed)

    duplicated = [candles[0], candles[0]]
    with pytest.raises(DataQualityError, match="duplicate timestamps"):
        build_candle_dataset_manifest(duplicated)


def test_clock_probe_classifies_offset_and_round_trip():
    started = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    synced = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=started,
        source_at=started + timedelta(milliseconds=50),
        response_received_at=started + timedelta(milliseconds=100),
    )
    warning = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=started,
        source_at=started + timedelta(milliseconds=800),
        response_received_at=started + timedelta(milliseconds=100),
    )
    unsafe = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=started,
        source_at=started + timedelta(seconds=4),
        response_received_at=started + timedelta(milliseconds=100),
    )

    assert synced.status == "SYNCED"
    assert synced.offset_ms == pytest.approx(0)
    assert warning.status == "WARNING"
    assert unsafe.status == "UNSAFE"


async def test_clock_observation_is_persisted_idempotently():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    started = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    observation = evaluate_clock_probe(
        source="bybit.server-time",
        request_started_at=started,
        source_at=started + timedelta(milliseconds=70),
        response_received_at=started + timedelta(milliseconds=100),
    )

    assert await repository.save_clock_observation(observation) is True
    assert await repository.save_clock_observation(observation) is False
    async with database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(ClockObservationModel)
        )
    await database.dispose()
    assert count == 1


def test_warehouse_models_target_internal_postgres_schema():
    assert CandleObservationModel.__table__.schema == INTERNAL_SCHEMA
    assert DatasetManifestModel.__table__.schema == INTERNAL_SCHEMA
    assert ClockObservationModel.__table__.schema == INTERNAL_SCHEMA

    ddl = str(
        CreateTable(CandleObservationModel.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert f"{INTERNAL_SCHEMA}.candle_observations" in ddl


async def test_protected_catalog_api_materializes_and_reads_manifest():
    admin_key = "c" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        assert context.repository is not None
        await context.repository.save_candles(
            make_series([100.0, 101.0, 102.0])
        )
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.post(
                "/api/v1/market/datasets",
                json={
                    "exchange": "BINANCE",
                    "symbol": "BTCUSDT",
                    "timeframe": "15m",
                },
            )
            created = await client.post(
                "/api/v1/market/datasets",
                headers={"X-API-Key": admin_key},
                json={
                    "exchange": "BINANCE",
                    "symbol": "BTCUSDT",
                    "timeframe": "15m",
                    "clock_status": "SYNCED",
                },
            )
            dataset_hash = created.json()["data"]["manifest"]["dataset_hash"]
            loaded = await client.get(
                f"/api/v1/market/datasets/{dataset_hash}",
                headers={"X-API-Key": admin_key},
            )

    assert denied.status_code == 401
    assert created.status_code == 200
    assert loaded.status_code == 200
    assert loaded.json()["data"]["manifest"]["dataset_hash"] == dataset_hash

"""Durable, immutable walk-forward artifact persistence tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateTable

from app.backtesting.artifacts import walk_forward_artifact_hash
from app.api.context import build_context
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import WalkForwardEngine
from app.core.errors import DatabaseError
from app.core.config import Settings
from app.database.models import (
    INTERNAL_SCHEMA,
    WalkForwardExperimentModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.schemas.backtest import (
    WalkForwardProtocol,
    WalkForwardRequest,
)
from app.tests.conftest import make_series


def _request() -> WalkForwardRequest:
    return WalkForwardRequest(
        candidate_version="SCALP_15M_v1",
        protocol=WalkForwardProtocol(
            train_candles=10,
            validation_candles=10,
            test_candles=10,
            embargo_candles=0,
            max_folds=1,
        ),
    )


async def _report():
    candles = make_series([100.0 + index * 0.1 for index in range(30)])
    return (
        await WalkForwardEngine(BacktestingEngine()).run(
            _request(),
            candles,
        ),
        candles,
    )


async def test_artifact_hash_ignores_runtime_metadata_but_covers_results():
    report, _ = await _report()
    runtime_only = report.model_copy(
        update={
            "duration_ms": report.duration_ms + 999,
            "created_at": report.created_at + timedelta(seconds=10),
        }
    )
    changed_aggregate = report.test_aggregate.model_copy(
        update={
            "mean_expectancy": report.test_aggregate.mean_expectancy + 1
        }
    )
    changed_result = report.model_copy(
        update={"test_aggregate": changed_aggregate}
    )

    assert walk_forward_artifact_hash(runtime_only) == report.artifact_hash
    assert walk_forward_artifact_hash(changed_result) != report.artifact_hash


async def test_repository_persists_and_reuses_identical_experiment():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    report, candles = await _report()

    stored = await repository.save_walk_forward_report(report)
    loaded = await repository.load_walk_forward_report(report.experiment_id)

    second_backtester = BacktestingEngine()

    async def fail_if_recomputed(*args, **kwargs):
        raise AssertionError("durable experiment should have been reused")

    second_backtester.run = fail_if_recomputed  # type: ignore[method-assign]
    reused = await WalkForwardEngine(
        second_backtester,
        repository=repository,
    ).run(_request(), list(reversed(candles)))
    listed = await repository.list_walk_forward_reports()
    async with database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(WalkForwardExperimentModel)
        )
    await database.dispose()

    assert stored == report
    assert loaded == report
    assert reused == report
    assert listed == [report]
    assert count == 1


async def test_repository_rejects_content_conflict_for_same_experiment():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    report, _ = await _report()
    await repository.save_walk_forward_report(report)

    changed_aggregate = report.test_aggregate.model_copy(
        update={
            "mean_expectancy": report.test_aggregate.mean_expectancy + 1
        }
    )
    conflicting = report.model_copy(
        update={
            "artifact_hash": "0" * 64,
            "test_aggregate": changed_aggregate,
        }
    )
    conflicting = conflicting.model_copy(
        update={
            "artifact_hash": walk_forward_artifact_hash(conflicting)
        }
    )

    with pytest.raises(DatabaseError, match="identity conflict"):
        await repository.save_walk_forward_report(conflicting)
    await database.dispose()


async def test_database_guards_reject_update_and_delete():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    report, _ = await _report()
    await repository.save_walk_forward_report(report)

    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(WalkForwardExperimentModel)
                .where(
                    WalkForwardExperimentModel.experiment_id
                    == report.experiment_id
                )
                .values(symbol="ETHUSDT")
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                delete(WalkForwardExperimentModel).where(
                    WalkForwardExperimentModel.experiment_id
                    == report.experiment_id
                )
            )
    await database.dispose()


def test_postgres_artifact_table_uses_internal_jsonb_schema():
    assert WalkForwardExperimentModel.__table__.schema == INTERNAL_SCHEMA
    ddl = str(
        CreateTable(WalkForwardExperimentModel.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    assert f"{INTERNAL_SCHEMA}.walk_forward_experiments" in ddl
    assert "JSONB" in ddl
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert "RESEARCH_ONLY" in ddl


async def test_api_reads_durable_artifact_after_memory_cache_is_cleared():
    api_key = "d" * 32
    settings = Settings(
        ADMIN_API_KEY=api_key,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        BACKFILL_WORKER_ENABLED=False,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    candles = make_series([100.0 + index * 0.1 for index in range(30)])
    body = {
        "candidate_version": "SCALP_15M_v1",
        "backtest": {
            "source": "inline",
            "candles": [
                candle.model_dump(mode="json")
                for candle in candles
            ],
        },
        "protocol": _request().protocol.model_dump(mode="json"),
    }
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        assert context.repository is not None
        assert context.walk_forward_engine._repository is context.repository
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/backtest/walk-forward",
                headers={"X-API-Key": api_key},
                json=body,
            )
            first_report = created.json()["data"]["report"]
            persisted_before_cache_clear = (
                await context.repository.list_walk_forward_reports()
            )
            context.walk_forward_engine.reports.clear()
            listed = await client.get(
                "/api/v1/backtest/walk-forward/reports"
            )
            loaded = await client.get(
                "/api/v1/backtest/walk-forward/reports/"
                f"{first_report['experiment_id']}"
            )
            repeated = await client.post(
                "/api/v1/backtest/walk-forward",
                headers={"X-API-Key": api_key},
                json=body,
            )

        assert context.database is not None
        async with context.database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(
                    WalkForwardExperimentModel
                )
            )

    assert created.status_code == 200
    assert persisted_before_cache_clear
    assert listed.json()["data"]["reports"][0]["artifact_hash"] == (
        first_report["artifact_hash"]
    )
    assert loaded.json()["data"]["report"] == first_report
    assert repeated.json()["data"]["report"] == first_report
    assert count == 1

"""Walk-forward protocol tests: temporal isolation, identity, and safety."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.context import build_context
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import (
    WalkForwardEngine,
    plan_walk_forward_windows,
)
from app.core.errors import DataQualityError
from app.core.config import Settings
from app.main import create_app
from app.schemas.backtest import (
    BacktestRequest,
    WalkForwardProtocol,
    WalkForwardRequest,
)
from app.tests.conftest import make_series


def test_protocol_rejects_overlapping_test_windows():
    with pytest.raises(ValidationError, match="test windows cannot overlap"):
        WalkForwardProtocol(test_candles=20, step_candles=19)


def test_rolling_windows_have_embargo_and_non_overlapping_tests():
    candles = make_series([100.0 + index for index in range(54)])
    protocol = WalkForwardProtocol(
        train_candles=20,
        validation_candles=10,
        test_candles=10,
        embargo_candles=2,
        step_candles=10,
        max_folds=2,
    )

    ordered, windows = plan_walk_forward_windows(candles, protocol)

    assert len(ordered) == 54
    assert len(windows) == 2
    first_train, first_validation, first_test = windows[0]
    second_train, second_validation, second_test = windows[1]
    assert (first_train.start_index, first_train.end_index_exclusive) == (0, 20)
    assert (first_validation.start_index, first_validation.end_index_exclusive) == (
        22,
        32,
    )
    assert (first_test.start_index, first_test.end_index_exclusive) == (34, 44)
    assert (second_train.start_index, second_train.end_index_exclusive) == (
        10,
        30,
    )
    assert (
        second_validation.start_index,
        second_validation.end_index_exclusive,
    ) == (32, 42)
    assert (second_test.start_index, second_test.end_index_exclusive) == (44, 54)
    assert first_test.end_index_exclusive <= second_test.start_index


def test_anchored_training_window_expands_without_moving_its_start():
    candles = make_series([100.0 + index for index in range(54)])
    protocol = WalkForwardProtocol(
        train_candles=20,
        validation_candles=10,
        test_candles=10,
        embargo_candles=2,
        step_candles=10,
        max_folds=2,
        anchored_train=True,
    )

    _, windows = plan_walk_forward_windows(candles, protocol)

    assert (windows[0][0].start_index, windows[0][0].end_index_exclusive) == (
        0,
        20,
    )
    assert (windows[1][0].start_index, windows[1][0].end_index_exclusive) == (
        0,
        30,
    )


def test_planner_fails_closed_for_insufficient_or_duplicate_data():
    protocol = WalkForwardProtocol(
        train_candles=20,
        validation_candles=10,
        test_candles=10,
        embargo_candles=2,
    )
    with pytest.raises(ValueError, match="requires at least 44"):
        plan_walk_forward_windows(
            make_series([100.0] * 43),
            protocol,
        )

    duplicated = make_series([100.0] * 44)
    duplicated[-1] = duplicated[-2]
    with pytest.raises(DataQualityError, match="duplicate"):
        plan_walk_forward_windows(duplicated, protocol)


async def test_walk_forward_is_reproducible_and_never_records_fold_backtests():
    closes = [100 * (1.002 ** index) for index in range(154)]
    candles = make_series(closes)
    backtesting = BacktestingEngine()
    engine = WalkForwardEngine(backtesting)
    request = WalkForwardRequest(
        candidate_version="SCALP_15M_v1",
        backtest=BacktestRequest(symbol="BTCUSDT", timeframe="15m"),
        protocol=WalkForwardProtocol(
            train_candles=30,
            validation_candles=60,
            test_candles=60,
            embargo_candles=2,
            max_folds=1,
        ),
    )

    first = await engine.run(request, candles)
    second = await engine.run(request, list(reversed(candles)))

    assert first.experiment_id == second.experiment_id
    assert first.simulation_context_hash == second.simulation_context_hash
    assert first.folds[0].fold_id == second.folds[0].fold_id
    assert first.validation_aggregate == second.validation_aggregate
    assert first.test_aggregate == second.test_aggregate
    assert first.promotion_status == "RESEARCH_ONLY"
    assert first.folds[0].train.candles == 30
    assert first.folds[0].validation_result.candles_processed == 60
    assert first.folds[0].test_result.candles_processed == 60
    assert (
        first.folds[0].validation.dataset_hash
        == first.folds[0].validation_result.dataset_hash
    )
    assert first.folds[0].test.dataset_hash == first.folds[0].test_result.dataset_hash
    # Internal fold runs must not pollute the ordinary backtest report history.
    assert backtesting.reports == []


async def test_walk_forward_rejects_mislabeled_candidate_and_series():
    candles = make_series([100 * (1.002 ** index) for index in range(154)])
    engine = WalkForwardEngine(BacktestingEngine())
    protocol = WalkForwardProtocol(
        train_candles=30,
        validation_candles=60,
        test_candles=60,
        embargo_candles=2,
        max_folds=1,
    )

    with pytest.raises(ValueError, match="candidate_version does not match"):
        await engine.run(
            WalkForwardRequest(
                candidate_version="SCALP_15M_v999",
                protocol=protocol,
            ),
            candles,
        )

    with pytest.raises(ValueError, match="does not match the candle dataset"):
        await engine.run(
            WalkForwardRequest(
                candidate_version="DAY_1H_v1",
                backtest=BacktestRequest(symbol="BTCUSDT", timeframe="1h"),
                protocol=protocol,
            ),
            candles,
        )


async def test_walk_forward_api_returns_compact_research_only_report():
    api_key = "w" * 32
    settings = Settings(ADMIN_API_KEY=api_key)
    context = build_context(settings, with_database=False)
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
        "protocol": {
            "train_candles": 10,
            "validation_candles": 10,
            "test_candles": 10,
            "embargo_candles": 0,
            "max_folds": 1,
        },
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        async with app.router.lifespan_context(app):
            response = await client.post(
                "/api/v1/backtest/walk-forward",
                headers={"X-API-Key": api_key},
                json=body,
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["data"]["report"]["promotion_status"] == "RESEARCH_ONLY"
    assert len(payload["data"]["report"]["folds"]) == 1

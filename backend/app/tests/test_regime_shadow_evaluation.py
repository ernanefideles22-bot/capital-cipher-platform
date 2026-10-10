from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agents.regime import classify_market_regime
from app.agents.regime_evaluation import RegimeShadowEvaluationService
from app.market_data.store import CandleStore
from app.schemas.common import Exchange, MarketRegime
from app.schemas.market import Candle


def _candle(index: int, *, shock: bool = False) -> Candle:
    closed_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
    base = 100.0 + index * 0.2
    if shock:
        return Candle(
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            timeframe="1h",
            open=base,
            high=base * 1.12,
            low=base * 0.88,
            close=base * 1.05,
            volume=1000,
            closed_at=closed_at,
            received_at=closed_at + timedelta(seconds=1),
        )
    return Candle(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        timeframe="1h",
        open=base,
        high=base * 1.002,
        low=base * 0.998,
        close=base * 1.001,
        volume=1000,
        closed_at=closed_at,
        received_at=closed_at + timedelta(seconds=1),
    )


def test_regime_classifier_is_undefined_without_minimum_history() -> None:
    result = classify_market_regime([_candle(i) for i in range(20)])
    assert result.regime == MarketRegime.UNDEFINED
    assert result.confidence == 0


def test_regime_shadow_specialist_requires_all_existing_quality_criteria() -> None:
    row = {
        "sample_count": 30,
        "accuracy": 0.60,
        "mean_brier_loss": 0.20,
        "mean_marginal_contribution": 0.03,
    }
    RegimeShadowEvaluationService._qualify_row(row)
    assert row["qualified_shadow_specialist"] is True
    assert row["status"] == "SHADOW_SPECIALIST"
    assert row["progress_percent"] == 100.0
    assert all(row["criteria"].values())

    row["mean_brier_loss"] = 0.25
    RegimeShadowEvaluationService._qualify_row(row)
    assert row["qualified_shadow_specialist"] is False
    assert row["status"] == "OBSERVED_NOT_QUALIFIED"
    assert row["criteria"]["brier_below_random_baseline"] is False


class _FakeEvaluationService:
    def __init__(self, forecast, outcome) -> None:
        self.forecast = forecast
        self.outcome = outcome

    async def scorecards(self):
        return [
            SimpleNamespace(
                agent_name="CandidateAgent",
                agent_version="1.0.0",
            )
        ]

    async def settled_history(self, *, agent_name: str, agent_version: str, limit: int = 10_000):
        assert agent_name == "CandidateAgent"
        assert agent_version == "1.0.0"
        return [(self.forecast, self.outcome)]


@pytest.mark.asyncio
async def test_regime_shadow_ignores_candles_after_forecast_time() -> None:
    store = CandleStore(max_candles=500)
    historical = [_candle(i) for i in range(60)]
    for candle in historical:
        store.add(candle)

    forecast_at = historical[-1].closed_at
    forecast = SimpleNamespace(
        symbol="BTCUSDT",
        timeframe="1h",
        forecast_at=forecast_at,
    )
    outcome = SimpleNamespace(
        correct=True,
        brier_loss=0.10,
        marginal_contribution=0.05,
    )

    # This future candle would force HIGH_VOLATILITY if look-ahead leaked into
    # the historical window, but it must be excluded by forecast_at.
    store.add(_candle(60, shock=True))

    expected = classify_market_regime(historical).regime.value
    assert expected != MarketRegime.HIGH_VOLATILITY.value

    service = RegimeShadowEvaluationService(
        _FakeEvaluationService(forecast, outcome),
        store,
    )
    report = await service.report(candidate_names={"CandidateAgent"})

    assert report["unavailable_historical_forecasts"] == 0
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["market_regime"] == expected
    assert row["market_regime"] != MarketRegime.HIGH_VOLATILITY.value
    assert row["accuracy"] == 1.0
    assert row["status"] == "COLLECTING"
    assert row["progress_percent"] == pytest.approx(100 / 30)
    assert row["rank_within_regime"] == 1
    assert row["criteria"]["minimum_sample_reached"] is False
    assert row["decision_authority"] is False
    assert row["automatic_weight_adjustment"] is False

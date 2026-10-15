"""Contract tests for the isolated forecast research ledger."""
import pytest

from app.agents.intelligence.forecast_memory_sqlite import ForecastMemorySQLite
from app.agents.intelligence.forecast_scoring import ForecastObservation


def test_durable_idempotent_research_ledger(tmp_path):
    path = tmp_path / "research.sqlite3"
    store = ForecastMemorySQLite(path)
    forecast = ForecastObservation(
        forecast_id="f1", symbol="BTCUSDT", timeframe="15m",
        model_version="v1", bias="BULLISH",
        reference_price=100.0, realized_price=110.0,
        forecast_at="2026-10-15T15:00:00Z",
        outcome_at="2026-10-15T15:15:00Z", probability_up=0.8,
    )
    assert store.append(forecast)
    assert not store.append(forecast)
    restored = ForecastMemorySQLite(path)
    assert restored.observations(symbol="BTCUSDT") == [forecast]
    assert restored.observations(symbol="ETHUSDT") == []
    result = restored.scorecard(minimum_sample=1)
    assert result.directional_accuracy == 1.0
    assert result.mean_brier_loss == pytest.approx(0.04)
    assert not result.order_authority
    with pytest.raises(ValueError, match="Conflicting"):
        restored.append(ForecastObservation(
            **{**forecast.__dict__, "realized_price": 90.0}
        ))

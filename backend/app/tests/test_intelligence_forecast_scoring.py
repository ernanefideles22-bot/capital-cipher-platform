"""Research-only scorecard tests: no exchange, network or DB dependencies."""
import pytest

from app.agents.intelligence.forecast_scoring import (
    ForecastObservation, score_forecasts,
)


def observation(identifier="f1", **kwargs):
    values = dict(
        forecast_id=identifier, symbol="BTCUSDT", timeframe="15m",
        model_version="reasoning-v1", bias="BULLISH",
        reference_price=100.0, realized_price=110.0,
        forecast_at="2026-10-15T15:00:00Z",
        outcome_at="2026-10-15T15:15:00Z", probability_up=0.8,
    )
    values.update(kwargs)
    return ForecastObservation(**values)


def test_scorecard_research_only_and_calibrated():
    result = score_forecasts([observation()], minimum_sample=1)
    assert result.status == "RESEARCH_ONLY"
    assert result.directional_accuracy == 1.0
    assert result.mean_brier_loss == pytest.approx(0.04)
    assert not result.decision_authority
    assert not result.order_authority


def test_insufficient_sample_and_missing_probability():
    result = score_forecasts([observation(probability_up=None)])
    assert result.status == "INSUFFICIENT_SAMPLE"
    assert result.mean_brier_loss is None


def test_mixed_or_flat_is_not_claimed_correct():
    result = score_forecasts([
        observation("a", bias="MIXED"),
        observation("b", realized_price=100.0),
    ])
    assert result.directional_sample_count == 0
    assert result.directional_accuracy is None


def test_duplicate_identity_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        score_forecasts([observation(), observation()])


@pytest.mark.parametrize("changes", [
    dict(reference_price=0),
    dict(realized_price=float("nan")),
    dict(probability_up=1.1),
    dict(forecast_at="2026-10-15T15:15:00Z"),
    dict(forecast_at="not-a-time"),
    dict(outcome_at="2026-10-15T15:15:00"),
])
def test_invalid_observations_rejected(changes):
    with pytest.raises(ValueError):
        observation(**changes)


def test_mixed_probability_is_scored_if_supplied():
    result = score_forecasts([observation(bias="MIXED", probability_up=0.5)])
    assert result.mean_brier_loss == pytest.approx(0.25)

"""Read-only, reproducible evaluation of directional market hypotheses.

Research-only metrics. No trading, order, risk or promotion authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

Bias = Literal["BULLISH", "BEARISH", "MIXED"]


@dataclass(frozen=True)
class ForecastObservation:
    """A forecast with a subsequently observed, fixed-horizon outcome."""

    forecast_id: str
    symbol: str
    timeframe: str
    model_version: str
    bias: Bias
    reference_price: float
    realized_price: float
    forecast_at: str
    outcome_at: str
    probability_up: float | None = None

    def __post_init__(self) -> None:
        if not all((self.forecast_id, self.symbol, self.timeframe,
                    self.model_version, self.forecast_at, self.outcome_at)):
            raise ValueError("Forecast identity and timestamps are required")
        if self.bias not in ("BULLISH", "BEARISH", "MIXED"):
            raise ValueError("Unsupported forecast bias")
        if not all(isfinite(v) and v > 0 for v in
                   (self.reference_price, self.realized_price)):
            raise ValueError("Prices must be finite and positive")
        if self.probability_up is not None and (
            not isfinite(self.probability_up)
            or not 0 <= self.probability_up <= 1
        ):
            raise ValueError("probability_up must be within [0, 1]")
        from datetime import datetime, timezone
        def parsed(value: str) -> datetime:
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Invalid ISO-8601 timestamp") from exc
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError("Timestamps must include timezone")
            return result.astimezone(timezone.utc)
        if parsed(self.outcome_at) <= parsed(self.forecast_at):
            raise ValueError("Outcome must be later than forecast")


@dataclass(frozen=True)
class ForecastScorecard:
    sample_count: int
    directional_sample_count: int
    directional_accuracy: float | None
    mean_brier_loss: float | None
    status: Literal["INSUFFICIENT_SAMPLE", "RESEARCH_ONLY"]
    decision_authority: bool = False
    order_authority: bool = False


def score_forecasts(
    observations: list[ForecastObservation] | tuple[ForecastObservation, ...],
    *,
    minimum_sample: int = 30,
) -> ForecastScorecard:
    """Score settled observations without granting decision authority.

    Flat returns are excluded from directional accuracy. Brier loss is
    computed only for explicitly supplied probabilities, never invented.
    """
    if minimum_sample < 1:
        raise ValueError("minimum_sample must be positive")
    if len({x.forecast_id for x in observations}) != len(observations):
        raise ValueError("Duplicate forecast_id")
    directional = [
        x for x in observations
        if x.bias != "MIXED" and x.realized_price != x.reference_price
    ]
    correct = sum(
        (x.bias == "BULLISH") == (x.realized_price > x.reference_price)
        for x in directional
    )
    probabilities = [
        (x.probability_up, float(x.realized_price > x.reference_price))
        for x in observations
        if x.probability_up is not None
        and x.realized_price != x.reference_price
    ]
    return ForecastScorecard(
        sample_count=len(observations),
        directional_sample_count=len(directional),
        directional_accuracy=correct / len(directional) if directional else None,
        mean_brier_loss=(
            sum((p - y) ** 2 for p, y in probabilities) / len(probabilities)
            if probabilities else None
        ),
        status=("RESEARCH_ONLY" if len(observations) >= minimum_sample
                else "INSUFFICIENT_SAMPLE"),
    )

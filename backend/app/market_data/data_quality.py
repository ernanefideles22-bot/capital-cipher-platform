"""Data quality validation (docs/32-data-quality.md).

Decisions based on bad data must be blocked. This module produces a
DataQualityReport with score 0-100; below the configured threshold the
decision chain must stop.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import ValidationError as PydanticValidationError

from app.schemas.common import DataQualityStatus
from app.schemas.market import Candle, DataQualityReport

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def validate_raw_candle(raw: dict) -> tuple[Candle | None, list[str]]:
    """Validate a raw candle dict against domain invariants.

    Returns (candle, errors). candle is None when invalid.
    """
    try:
        candle = Candle(**raw)
        return candle, []
    except PydanticValidationError as exc:
        return None, [e.get("msg", "invalid") for e in exc.errors()]


def evaluate_candles(
    candles: list[Candle],
    *,
    timeframe: str,
    max_delay_ms: int = 5000,
    now: datetime | None = None,
    check_staleness: bool = True,
) -> DataQualityReport:
    """Score a candle series: gaps, ordering, staleness, outliers (docs/32)."""
    warnings: list[str] = []
    errors: list[str] = []
    score = 100

    if not candles:
        return DataQualityReport(
            data_quality_score=0,
            status=DataQualityStatus.INVALID.value,
            errors=["NO_CANDLES"],
        )

    # Temporal ordering is mandatory.
    timestamps = [c.closed_at for c in candles]
    if timestamps != sorted(timestamps):
        errors.append("CANDLES_OUT_OF_ORDER")
        score -= 50

    # Duplicates (exchange+symbol+timeframe+closed_at).
    keys = {(c.exchange, c.symbol, c.timeframe, c.closed_at) for c in candles}
    if len(keys) != len(candles):
        warnings.append("DUPLICATE_CANDLES")
        score -= 10

    # Gap detection.
    step = TIMEFRAME_SECONDS.get(timeframe)
    if step:
        for prev, curr in zip(candles, candles[1:]):
            delta = (curr.closed_at - prev.closed_at).total_seconds()
            if delta > step * 1.5:
                warnings.append("GAP_DETECTED")
                score -= 15
                break

    # Staleness of the most recent candle.
    if check_staleness:
        now = now or datetime.now(timezone.utc)
        last = candles[-1]
        lag_ms = (now - last.closed_at).total_seconds() * 1000
        if step and lag_ms > (step * 1000) + max_delay_ms:
            warnings.append("STALE_DATA")
            score -= 20

    # Outlier detection: candle range far beyond recent average.
    if len(candles) >= 10:
        ranges = [c.high - c.low for c in candles[-10:]]
        avg_range = sum(ranges[:-1]) / max(len(ranges) - 1, 1)
        if avg_range > 0 and ranges[-1] > avg_range * 5:
            warnings.append("DATA_ANOMALY")
            score -= 10

    score = max(0, min(100, score))
    if errors or score < 60:
        status = DataQualityStatus.INVALID if errors else DataQualityStatus.SUSPECT
    elif warnings:
        status = DataQualityStatus.WARNING
    else:
        status = DataQualityStatus.VALID

    return DataQualityReport(
        data_quality_score=score, status=status.value, warnings=warnings, errors=errors
    )

"""Deterministic market-data gap detection and persistence coordination."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.core.errors import DataQualityError
from app.market_data.data_quality import TIMEFRAME_SECONDS
from app.schemas.backfill import MarketDataGap
from app.schemas.market import Candle


class GapRepository(Protocol):
    async def save_market_data_gaps(self, gaps: list[MarketDataGap]) -> int: ...

    async def resolve_market_data_gaps(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        unresolved_gap_ids: set[str],
        backfill_job_id: str | None = None,
    ) -> int: ...

    async def list_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100_000,
    ) -> list[Candle]: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("gap boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


def _gap_id(
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> str:
    identity = "|".join(
        (
            exchange.upper(),
            symbol.upper(),
            timeframe,
            _utc(start_at).isoformat(timespec="microseconds"),
            _utc(end_at).isoformat(timespec="microseconds"),
        )
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def detect_candle_gaps(
    candles: list[Candle],
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> list[MarketDataGap]:
    """Find missing close timestamps in an inclusive, bounded series range."""
    start_at = _utc(start_at)
    end_at = _utc(end_at)
    if start_at > end_at:
        raise ValueError("start_at must not be after end_at")
    step_seconds = TIMEFRAME_SECONDS.get(timeframe)
    if step_seconds is None:
        raise DataQualityError(f"Unsupported timeframe for gap detection: {timeframe}")
    step = timedelta(seconds=step_seconds)

    selected = sorted(
        (
            candle
            for candle in candles
            if candle.exchange.value == exchange.upper()
            and candle.symbol == symbol.upper()
            and candle.timeframe == timeframe
            and start_at <= candle.closed_at <= end_at
        ),
        key=lambda candle: candle.closed_at,
    )
    timestamps = [candle.closed_at.astimezone(timezone.utc) for candle in selected]
    if len(timestamps) != len(set(timestamps)):
        raise DataQualityError("Cannot scan gaps in a series with duplicate timestamps")

    ranges: list[tuple[datetime, datetime, int]] = []
    if not timestamps:
        missing = int((end_at - start_at) // step) + 1
        ranges.append((start_at, start_at + step * (missing - 1), missing))
    else:
        if timestamps[0] > start_at:
            missing = int((timestamps[0] - start_at) // step)
            if missing:
                ranges.append((start_at, start_at + step * (missing - 1), missing))

        for previous, current in zip(timestamps, timestamps[1:]):
            ratio = (current - previous).total_seconds() / step_seconds
            missing = max(0, int(round(ratio)) - 1)
            if missing:
                gap_start = previous + step
                ranges.append((gap_start, gap_start + step * (missing - 1), missing))

        if timestamps[-1] < end_at:
            missing = int((end_at - timestamps[-1]) // step)
            if missing:
                gap_start = timestamps[-1] + step
                ranges.append((gap_start, gap_start + step * (missing - 1), missing))

    return [
        MarketDataGap(
            gap_id=_gap_id(
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                start_at=gap_start,
                end_at=gap_end,
            ),
            exchange=exchange.upper(),
            symbol=symbol.upper(),
            timeframe=timeframe,
            start_at=gap_start,
            end_at=gap_end,
            missing_count=missing,
        )
        for gap_start, gap_end, missing in ranges
    ]


class GapService:
    def __init__(self, repository: GapRepository) -> None:
        self._repository = repository

    async def scan(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        limit: int = 100_000,
        backfill_job_id: str | None = None,
    ) -> list[MarketDataGap]:
        step_seconds = TIMEFRAME_SECONDS.get(timeframe)
        if step_seconds is None:
            raise DataQualityError(
                f"Unsupported timeframe for gap detection: {timeframe}"
            )
        expected_count = (
            int(
                (
                    _utc(end_at) - _utc(start_at)
                ).total_seconds()
                // step_seconds
            )
            + 1
        )
        if expected_count > limit:
            raise DataQualityError(
                "Gap scan limit is smaller than the requested range",
                metadata={
                    "expected_count": expected_count,
                    "limit": limit,
                },
            )
        candles = await self._repository.list_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        gaps = detect_candle_gaps(
            candles,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            start_at=start_at,
            end_at=end_at,
        )
        if backfill_job_id is not None:
            gaps = [
                gap.model_copy(update={"backfill_job_id": backfill_job_id})
                for gap in gaps
            ]
        await self._repository.save_market_data_gaps(gaps)
        await self._repository.resolve_market_data_gaps(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            start_at=start_at,
            end_at=end_at,
            unresolved_gap_ids={gap.gap_id for gap in gaps},
            backfill_job_id=backfill_job_id,
        )
        return gaps

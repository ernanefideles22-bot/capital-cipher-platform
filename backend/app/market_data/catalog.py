"""Deterministic candle dataset catalog backed by the time-series repository."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.core.errors import DataQualityError
from app.market_data.data_quality import evaluate_candles
from app.market_data.identity import candle_dataset_hash
from app.schemas.data_catalog import CandleDatasetManifest
from app.schemas.market import Candle


class CatalogRepository(Protocol):
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

    async def save_dataset_manifest(
        self, manifest: CandleDatasetManifest
    ) -> bool: ...


def build_candle_dataset_manifest(
    candles: list[Candle],
    *,
    clock_status: str = "UNKNOWN",
) -> CandleDatasetManifest:
    if not candles:
        raise DataQualityError("Cannot catalog an empty candle dataset")

    ordered = sorted(
        candles,
        key=lambda candle: (
            candle.closed_at,
            candle.exchange.value,
            candle.symbol,
            candle.timeframe,
        ),
    )
    first = ordered[0]
    series = {
        (candle.exchange, candle.symbol, candle.timeframe)
        for candle in ordered
    }
    if len(series) != 1:
        raise DataQualityError("A candle manifest must contain exactly one series")
    timestamps = [candle.closed_at for candle in ordered]
    if len(set(timestamps)) != len(timestamps):
        raise DataQualityError("A candle manifest cannot contain duplicate timestamps")

    dataset_hash = candle_dataset_hash(ordered)
    quality = evaluate_candles(
        ordered,
        timeframe=first.timeframe,
        check_staleness=False,
    )
    return CandleDatasetManifest(
        dataset_id=f"candles:v1:{dataset_hash}",
        dataset_hash=dataset_hash,
        exchange=first.exchange,
        symbol=first.symbol,
        timeframe=first.timeframe,
        start_at=ordered[0].closed_at,
        end_at=ordered[-1].closed_at,
        row_count=len(ordered),
        selection={
            "order": ["closed_at", "exchange", "symbol", "timeframe"],
            "start_at": ordered[0].closed_at.isoformat(),
            "end_at": ordered[-1].closed_at.isoformat(),
        },
        quality_summary=quality.model_dump(mode="json"),
        clock_status=clock_status,
    )


class DataCatalog:
    def __init__(self, repository: CatalogRepository) -> None:
        self._repository = repository

    async def catalog_candles(
        self,
        candles: list[Candle],
        *,
        clock_status: str = "UNKNOWN",
    ) -> CandleDatasetManifest:
        manifest = build_candle_dataset_manifest(
            candles,
            clock_status=clock_status,
        )
        await self._repository.save_dataset_manifest(manifest)
        return manifest

    async def materialize_candle_dataset(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100_000,
        clock_status: str = "UNKNOWN",
    ) -> CandleDatasetManifest:
        candles = await self._repository.list_candles(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        )
        return await self.catalog_candles(
            candles,
            clock_status=clock_status,
        )

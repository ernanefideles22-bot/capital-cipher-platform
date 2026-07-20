"""Public-only REST adapter contract for clock probes and historical candles."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.schemas.common import Exchange
from app.schemas.data_catalog import ClockObservation
from app.schemas.market import Candle


class PublicMarketDataClient(Protocol):
    exchange: Exchange
    source_name: str

    async def probe_clock(
        self,
        *,
        warning_offset_ms: float = 500.0,
        unsafe_offset_ms: float = 2_000.0,
        warning_round_trip_ms: float = 1_000.0,
        unsafe_round_trip_ms: float = 5_000.0,
    ) -> ClockObservation: ...

    async def fetch_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        limit: int,
    ) -> list[Candle]: ...

    async def aclose(self) -> None: ...

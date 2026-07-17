"""Market data adapter interface (docs/33-market-data-adapters.md).

The rest of the system must not depend on exchange-specific formats. Adapters
normalize everything into internal contracts (Candle / MarketEvent).
"""

from __future__ import annotations

import abc
from typing import Awaitable, Callable

from app.schemas.market import Candle, RawMarketEvent

CandleHandler = Callable[[Candle], Awaitable[None]]
RawMarketEventHandler = Callable[[RawMarketEvent], Awaitable[None]]
StatusHandler = Callable[[str, dict], Awaitable[None]]


class MarketDataAdapter(abc.ABC):
    """Common interface for all market data sources."""

    exchange_name: str = "UNKNOWN"

    def __init__(self) -> None:
        self.on_candle: CandleHandler | None = None
        self.on_raw_event: RawMarketEventHandler | None = None
        self.on_status: StatusHandler | None = None
        self.connected: bool = False

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def subscribe_candles(self, symbol: str, timeframe: str) -> None: ...

    async def _emit_candle(self, candle: Candle) -> None:
        if self.on_candle is not None:
            await self.on_candle(candle)

    async def _emit_raw_event(self, event: RawMarketEvent) -> None:
        if self.on_raw_event is not None:
            await self.on_raw_event(event)

    async def _emit_status(self, event_type: str, payload: dict) -> None:
        if self.on_status is not None:
            await self.on_status(event_type, payload)

"""Replay adapter: re-emit candles already stored in memory/DB (docs/33)."""

from __future__ import annotations

from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.market import Candle


class ReplayMarketDataAdapter(MarketDataAdapter):
    exchange_name = "REPLAY"

    def __init__(self, candles: list[Candle]) -> None:
        super().__init__()
        self._candles = sorted(candles, key=lambda c: c.closed_at)

    async def connect(self) -> None:
        self.connected = True
        await self._emit_status("MARKET_CONNECTED", {"source": "replay"})

    async def disconnect(self) -> None:
        self.connected = False
        await self._emit_status("MARKET_DISCONNECTED", {"source": "replay"})

    async def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        """No-op for replay."""

    async def replay(self) -> None:
        for candle in self._candles:
            await self._emit_candle(candle)

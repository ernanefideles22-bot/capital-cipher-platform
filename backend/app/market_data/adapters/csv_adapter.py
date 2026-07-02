"""CSV historical data adapter for backtesting (docs/33, docs/17)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.market import Candle


class CsvMarketDataAdapter(MarketDataAdapter):
    """Loads historical candles from CSV files.

    Expected columns: exchange,symbol,timeframe,open,high,low,close,volume,closed_at
    """

    exchange_name = "CSV"

    def __init__(self, file_path: str | Path) -> None:
        super().__init__()
        self._file_path = Path(file_path)

    async def connect(self) -> None:
        self.connected = True
        await self._emit_status("MARKET_CONNECTED", {"source": str(self._file_path)})

    async def disconnect(self) -> None:
        self.connected = False
        await self._emit_status("MARKET_DISCONNECTED", {"source": str(self._file_path)})

    async def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        """No-op: CSV replays everything via replay()."""

    def load_candles(self) -> list[Candle]:
        candles: list[Candle] = []
        with self._file_path.open() as fh:
            for row in csv.DictReader(fh):
                closed_at = datetime.fromisoformat(row["closed_at"])
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=timezone.utc)
                candles.append(
                    Candle(
                        exchange=Exchange(row.get("exchange", "BINANCE").upper()),
                        symbol=row["symbol"].upper(),
                        timeframe=row["timeframe"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        closed_at=closed_at,
                    )
                )
        candles.sort(key=lambda c: c.closed_at)
        return candles

    async def replay(self) -> None:
        """Emit candles sequentially, preserving temporal order (no lookahead)."""
        for candle in self.load_candles():
            await self._emit_candle(candle)

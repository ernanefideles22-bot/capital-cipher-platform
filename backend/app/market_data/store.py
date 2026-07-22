"""In-memory candle store used by agents and the orchestrator.

Keeps a bounded window of recent candles per (exchange, symbol, timeframe).
Persistence to the database is done separately by the audit/persistence layer.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Deque

from app.core.errors import DataQualityError
from app.schemas.market import Candle

MAX_CANDLES = 500


class CandleStore:
    def __init__(self, max_candles: int = MAX_CANDLES) -> None:
        self._store: dict[tuple[str, str, str], Deque[Candle]] = defaultdict(
            lambda: deque(maxlen=max_candles)
        )

    @staticmethod
    def _key(exchange: str, symbol: str, timeframe: str) -> tuple[str, str, str]:
        return (exchange.upper(), symbol.upper(), timeframe)

    def add(self, candle: Candle) -> bool:
        key = self._key(candle.exchange.value, candle.symbol, candle.timeframe)
        bucket = self._store[key]
        # Idempotency: skip duplicate closed_at (docs/32 duplication rule).
        if bucket and bucket[-1].closed_at == candle.closed_at:
            return False
        if bucket and candle.closed_at < bucket[-1].closed_at:
            raise DataQualityError(
                "Out-of-order candle rejected",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "closed_at": candle.closed_at.isoformat(),
                    "latest_closed_at": bucket[-1].closed_at.isoformat(),
                },
            )
        bucket.append(candle)
        return True

    def get(self, exchange: str, symbol: str, timeframe: str, limit: int = 100) -> list[Candle]:
        bucket = self._store.get(self._key(exchange, symbol, timeframe), deque())
        return list(bucket)[-limit:]

    def latest(self, exchange: str, symbol: str, timeframe: str) -> Candle | None:
        bucket = self._store.get(self._key(exchange, symbol, timeframe))
        return bucket[-1] if bucket else None

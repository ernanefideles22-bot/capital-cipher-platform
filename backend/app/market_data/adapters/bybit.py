"""Bybit public WebSocket adapter (docs/33-market-data-adapters.md)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.core.logging import ServiceLogger
from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.market import Candle

logger = ServiceLogger("bybit_adapter")

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

TIMEFRAME_TO_BYBIT = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
BYBIT_TO_TIMEFRAME = {v: k for k, v in TIMEFRAME_TO_BYBIT.items()}


def normalize_kline(message: dict) -> list[Candle]:
    """Normalize Bybit v5 kline messages into internal Candle contracts."""
    topic = message.get("topic", "")
    if not topic.startswith("kline."):
        return []
    parts = topic.split(".")
    if len(parts) != 3:
        return []
    interval, symbol = parts[1], parts[2]
    timeframe = BYBIT_TO_TIMEFRAME.get(interval)
    if timeframe is None:
        return []
    candles: list[Candle] = []
    for item in message.get("data", []):
        if not item.get("confirm"):
            continue
        candles.append(
            Candle(
                exchange=Exchange.BYBIT,
                symbol=symbol.upper(),
                timeframe=timeframe,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=float(item["volume"]),
                closed_at=datetime.fromtimestamp(int(item["end"]) / 1000, tz=timezone.utc),
            )
        )
    return candles


class BybitMarketDataAdapter(MarketDataAdapter):
    exchange_name = "BYBIT"

    def __init__(self, max_retries: int = 10) -> None:
        super().__init__()
        self._subscriptions: set[tuple[str, str]] = set()
        self._task: asyncio.Task | None = None
        self._max_retries = max_retries
        self._stop = asyncio.Event()

    async def connect(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            self._task = None
        self.connected = False
        await self._emit_status("MARKET_DISCONNECTED", {"exchange": self.exchange_name})

    async def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        if timeframe not in TIMEFRAME_TO_BYBIT:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self._subscriptions.add((symbol.upper(), timeframe))

    async def _run(self) -> None:
        import websockets

        retries = 0
        while not self._stop.is_set() and retries <= self._max_retries:
            try:
                async with websockets.connect(BYBIT_WS_URL, ping_interval=20) as ws:
                    retries = 0
                    self.connected = True
                    args = [
                        f"kline.{TIMEFRAME_TO_BYBIT[tf]}.{symbol}"
                        for symbol, tf in sorted(self._subscriptions)
                    ]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    await self._emit_status("MARKET_CONNECTED", {"exchange": self.exchange_name})
                    async for raw in ws:
                        message = json.loads(raw)
                        for candle in normalize_kline(message):
                            await self._emit_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                retries += 1
                backoff = min(2 ** retries, 60)
                logger.warning(
                    f"Bybit WS error, reconnecting in {backoff}s",
                    event_type="MARKET_DISCONNECTED",
                    metadata={"retries": retries, "error": str(exc)},
                )
                await self._emit_status(
                    "MARKET_DISCONNECTED", {"exchange": self.exchange_name, "error": str(exc)}
                )
                await asyncio.sleep(backoff)

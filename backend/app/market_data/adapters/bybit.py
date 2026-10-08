"""Bybit public WebSocket adapter (docs/33-market-data-adapters.md)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.core.logging import ServiceLogger
from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.market import Candle, RawMarketEvent

logger = ServiceLogger("bybit_adapter")

BYBIT_MAINNET_WS_URL = "wss://stream.bybit.com/v5/public/linear"
BYBIT_TESTNET_WS_URL = "wss://stream-testnet.bybit.com/v5/public/linear"
BYBIT_WS_URL = BYBIT_MAINNET_WS_URL
_ALLOWED_WS_URLS = frozenset({BYBIT_MAINNET_WS_URL, BYBIT_TESTNET_WS_URL})

TIMEFRAME_TO_BYBIT = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
BYBIT_TO_TIMEFRAME = {v: k for k, v in TIMEFRAME_TO_BYBIT.items()}


def build_raw_kline_event(message: dict) -> RawMarketEvent | None:
    """Wrap the untouched Bybit payload in the versioned ingestion contract."""
    topic = message.get("topic", "")
    if not isinstance(topic, str) or not topic.startswith("kline."):
        return None
    parts = topic.split(".")
    symbol = parts[2].upper() if len(parts) == 3 else None
    event_millis = message.get("ts")
    if event_millis is None and message.get("data"):
        event_millis = message["data"][0].get("end")
    occurred_at = None
    if event_millis is not None:
        occurred_at = datetime.fromtimestamp(int(event_millis) / 1000, tz=timezone.utc)
    return RawMarketEvent(
        source="bybit.public.websocket",
        exchange=Exchange.BYBIT,
        event_type="BYBIT_KLINE",
        symbol=symbol,
        occurred_at=occurred_at,
        payload=message,
    )


def normalize_kline(message: dict) -> list[Candle]:
    """Normalize confirmed Bybit v5 kline messages into internal candles."""
    topic = message.get("topic", "")
    if not isinstance(topic, str) or not topic.startswith("kline."):
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
                closed_at=datetime.fromtimestamp(
                    int(item["end"]) / 1000,
                    tz=timezone.utc,
                ),
            )
        )
    return candles


class BybitMarketDataAdapter(MarketDataAdapter):
    exchange_name = "BYBIT"

    def __init__(
        self,
        max_retries: int = 10,
        *,
        ws_url: str = BYBIT_MAINNET_WS_URL,
        heartbeat_seconds: float = 20.0,
    ) -> None:
        super().__init__()
        normalized_url = ws_url.rstrip("/")
        if normalized_url not in _ALLOWED_WS_URLS:
            raise ValueError("Bybit WebSocket URL must be an official linear endpoint")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self._ws_url = normalized_url
        self._subscriptions: set[tuple[str, str]] = set()
        self._task: asyncio.Task | None = None
        self._max_retries = max_retries
        self._heartbeat_seconds = heartbeat_seconds
        self._stop = asyncio.Event()

    @property
    def ws_url(self) -> str:
        return self._ws_url

    async def connect(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False
        await self._emit_status("MARKET_DISCONNECTED", {"exchange": self.exchange_name})

    async def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        if timeframe not in TIMEFRAME_TO_BYBIT:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self._subscriptions.add((symbol.upper(), timeframe))

    def _connect(self):
        import websockets

        # Bybit documents an application-level JSON ping. Disable the library's
        # independent protocol-ping timer to avoid competing heartbeat policies.
        return websockets.connect(self._ws_url, ping_interval=None)

    async def _heartbeat(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._heartbeat_seconds)
            if self._stop.is_set():
                return
            await ws.send(json.dumps({"op": "ping"}))

    async def _run(self) -> None:
        retries = 0
        while not self._stop.is_set() and retries <= self._max_retries:
            heartbeat_task: asyncio.Task | None = None
            try:
                async with self._connect() as ws:
                    retries = 0
                    self.connected = True
                    args = [
                        f"kline.{TIMEFRAME_TO_BYBIT[tf]}.{symbol}"
                        for symbol, tf in sorted(self._subscriptions)
                    ]
                    if not args:
                        raise RuntimeError("Bybit market adapter has no subscriptions")
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    await self._emit_status(
                        "MARKET_CONNECTED",
                        {"exchange": self.exchange_name},
                    )
                    async for raw in ws:
                        message = json.loads(raw)
                        # Subscription acknowledgements and pong frames carry no
                        # market-data topic and are deliberately ignored here.
                        raw_event = build_raw_kline_event(message)
                        if raw_event is not None:
                            await self._emit_raw_event(raw_event)
                        for candle in normalize_kline(message):
                            await self._emit_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                retries += 1
                if retries > self._max_retries:
                    logger.error(
                        "Bybit WS retry budget exhausted",
                        event_type="MARKET_DISCONNECTED",
                        metadata={"retries": retries, "error": str(exc)},
                    )
                    await self._emit_status(
                        "MARKET_DISCONNECTED",
                        {
                            "exchange": self.exchange_name,
                            "error": str(exc),
                            "retry_exhausted": True,
                        },
                    )
                    return
                backoff = min(2 ** retries, 60)
                logger.warning(
                    f"Bybit WS error, reconnecting in {backoff}s",
                    event_type="MARKET_DISCONNECTED",
                    metadata={"retries": retries, "error": str(exc)},
                )
                await self._emit_status(
                    "MARKET_DISCONNECTED",
                    {"exchange": self.exchange_name, "error": str(exc)},
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

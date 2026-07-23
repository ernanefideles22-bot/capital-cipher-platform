"""Binance public WebSocket adapter (docs/33-market-data-adapters.md).

Phase 1 rules: public data only, no API keys, automatic reconnection with
exponential backoff, normalization to the internal Candle contract.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.core.logging import ServiceLogger
from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.market import Candle, RawMarketEvent

logger = ServiceLogger("binance_adapter")

BINANCE_WS_BASE = "wss://stream.binance.com:9443"

# Binance interval strings match our timeframes for the supported set.
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}


def _stream_payload(message: dict) -> dict:
    """Return data from an official combined-stream envelope."""

    data = message.get("data")
    if isinstance(message.get("stream"), str) and isinstance(data, dict):
        return data
    return message


def build_raw_kline_event(payload: dict) -> RawMarketEvent | None:
    """Wrap the untouched Binance payload in the versioned ingestion contract."""
    stream_payload = _stream_payload(payload)
    kline = stream_payload.get("k")
    if not isinstance(kline, dict):
        return None
    event_millis = stream_payload.get("E") or kline.get("T")
    occurred_at = None
    if event_millis is not None:
        occurred_at = datetime.fromtimestamp(int(event_millis) / 1000, tz=timezone.utc)
    symbol = str(kline["s"]).upper() if kline.get("s") else None
    return RawMarketEvent(
        source="binance.public.websocket",
        exchange=Exchange.BINANCE,
        event_type="BINANCE_KLINE",
        symbol=symbol,
        occurred_at=occurred_at,
        payload=payload,
    )


def normalize_kline(payload: dict) -> Candle | None:
    """Normalize a Binance kline message into the internal Candle contract.

    Only closed candles (k.x == true) become CANDLE_CLOSED events.
    Field mapping (docs/33): s->symbol, o->open, h->high, l->low, c->close, v->volume.
    """
    kline = _stream_payload(payload).get("k") or {}
    if not kline.get("x"):  # candle not closed yet
        return None
    return Candle(
        exchange=Exchange.BINANCE,
        symbol=str(kline["s"]).upper(),
        timeframe=str(kline["i"]),
        open=float(kline["o"]),
        high=float(kline["h"]),
        low=float(kline["l"]),
        close=float(kline["c"]),
        volume=float(kline["v"]),
        closed_at=datetime.fromtimestamp(int(kline["T"]) / 1000, tz=timezone.utc),
    )


class BinanceMarketDataAdapter(MarketDataAdapter):
    exchange_name = "BINANCE"

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
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        self._subscriptions.add((symbol.upper(), timeframe))

    def _stream_url(self) -> str:
        streams = "/".join(
            f"{symbol.lower()}@kline_{tf}" for symbol, tf in sorted(self._subscriptions)
        )
        return f"{BINANCE_WS_BASE}/stream?streams={streams}"

    async def _run(self) -> None:
        import websockets  # local import: optional dependency at test time

        retries = 0
        while not self._stop.is_set() and retries <= self._max_retries:
            try:
                async with websockets.connect(self._stream_url(), ping_interval=20) as ws:
                    retries = 0
                    self.connected = True
                    await self._emit_status(
                        "MARKET_CONNECTED", {"exchange": self.exchange_name}
                    )
                    async for raw in ws:
                        message = json.loads(raw)
                        raw_event = build_raw_kline_event(message)
                        if raw_event is not None:
                            await self._emit_raw_event(raw_event)
                        candle = normalize_kline(message)
                        if candle is not None:
                            await self._emit_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                retries += 1
                backoff = min(2 ** retries, 60)
                logger.warning(
                    f"Binance WS error, reconnecting in {backoff}s",
                    event_type="MARKET_DISCONNECTED",
                    metadata={"retries": retries, "error": str(exc)},
                )
                await self._emit_status(
                    "MARKET_DISCONNECTED", {"exchange": self.exchange_name, "error": str(exc)}
                )
                await asyncio.sleep(backoff)

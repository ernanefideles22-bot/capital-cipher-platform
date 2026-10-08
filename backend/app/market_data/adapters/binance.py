"""Binance public WebSocket adapter (docs/33-market-data-adapters.md).

Phase 1 rules: public data only, no API keys, automatic reconnection with
exponential backoff, normalization to the internal Candle contract.
"""

from __future__ import annotations

import asyncio
import json
import zlib
from datetime import datetime, timezone
from time import monotonic

from app.core.logging import ServiceLogger
from app.market_data.adapters.base import MarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.market import Candle, RawMarketEvent

logger = ServiceLogger("binance_adapter")

BINANCE_WS_BASE = "wss://stream.binance.com:9443"

SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
DEFAULT_CANDLE_QUEUE_SIZE = 64
DEFAULT_RAW_QUEUE_SIZE = 128
DEFAULT_RAW_QUEUE_SHARDS = 8
DEFAULT_RAW_BATCH_SIZE = 16


def _stream_payload(message: dict) -> dict:
    data = message.get("data")
    if isinstance(message.get("stream"), str) and isinstance(data, dict):
        return data
    return message


def build_raw_kline_event(payload: dict) -> RawMarketEvent | None:
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


def normalize_kline(
    payload: dict,
    *,
    received_at: datetime | None = None,
) -> Candle | None:
    kline = _stream_payload(payload).get("k") or {}
    if not kline.get("x"):
        return None
    candle_data = {
        "exchange": Exchange.BINANCE,
        "symbol": str(kline["s"]).upper(),
        "timeframe": str(kline["i"]),
        "open": float(kline["o"]),
        "high": float(kline["h"]),
        "low": float(kline["l"]),
        "close": float(kline["c"]),
        "volume": float(kline["v"]),
        "closed_at": datetime.fromtimestamp(int(kline["T"]) / 1000, tz=timezone.utc),
    }
    if received_at is not None:
        candle_data["received_at"] = received_at
    return Candle(**candle_data)


class BinanceMarketDataAdapter(MarketDataAdapter):
    exchange_name = "BINANCE"

    def __init__(
        self,
        max_retries: int = 10,
        *,
        candle_queue_size: int = DEFAULT_CANDLE_QUEUE_SIZE,
        raw_queue_size: int = DEFAULT_RAW_QUEUE_SIZE,
        raw_queue_shards: int = DEFAULT_RAW_QUEUE_SHARDS,
        raw_batch_size: int = DEFAULT_RAW_BATCH_SIZE,
    ) -> None:
        super().__init__()
        if candle_queue_size < 1:
            raise ValueError("candle_queue_size must be positive")
        if raw_queue_size < 1:
            raise ValueError("raw_queue_size must be positive")
        if raw_queue_shards < 1:
            raise ValueError("raw_queue_shards must be positive")
        if raw_batch_size < 1:
            raise ValueError("raw_batch_size must be positive")
        self._subscriptions: set[tuple[str, str]] = set()
        self._task: asyncio.Task | None = None
        self._candle_dispatch_task: asyncio.Task | None = None
        self._raw_dispatch_tasks: list[asyncio.Task] = []
        self._max_retries = max_retries
        self._raw_batch_size = raw_batch_size
        self._stop = asyncio.Event()
        self._candle_queue: asyncio.Queue[Candle] = asyncio.Queue(
            maxsize=candle_queue_size
        )
        self._raw_queues: list[
            asyncio.Queue[tuple[RawMarketEvent, dict]]
        ] = [
            asyncio.Queue(maxsize=raw_queue_size)
            for _ in range(raw_queue_shards)
        ]

    @property
    def pending_candles(self) -> int:
        return self._candle_queue.qsize()

    @property
    def pending_raw_events(self) -> int:
        return sum(queue.qsize() for queue in self._raw_queues)

    def _raw_queue_index(self, symbol: str | None) -> int:
        if not symbol:
            return 0
        return zlib.crc32(symbol.encode("utf-8")) % len(self._raw_queues)

    async def _queue_raw_message(self, message: dict) -> bool:
        raw_event = build_raw_kline_event(message)
        if raw_event is None:
            return False
        queue = self._raw_queues[self._raw_queue_index(raw_event.symbol)]
        await queue.put((raw_event, message))
        return True

    def _drain_raw_cohort(
        self,
        queue: asyncio.Queue[tuple[RawMarketEvent, dict]],
        first: tuple[RawMarketEvent, dict],
    ) -> list[tuple[RawMarketEvent, dict]]:
        cohort = [first]
        while len(cohort) < self._raw_batch_size:
            try:
                cohort.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return cohort

    async def connect(self) -> None:
        self._stop.clear()
        if self._candle_dispatch_task is None or self._candle_dispatch_task.done():
            self._candle_dispatch_task = asyncio.create_task(self._dispatch_candles())
        if not self._raw_dispatch_tasks or any(
            task.done() for task in self._raw_dispatch_tasks
        ):
            for task in self._raw_dispatch_tasks:
                task.cancel()
            if self._raw_dispatch_tasks:
                await asyncio.gather(
                    *self._raw_dispatch_tasks,
                    return_exceptions=True,
                )
            self._raw_dispatch_tasks = [
                asyncio.create_task(self._dispatch_raw_messages(queue, shard))
                for shard, queue in enumerate(self._raw_queues)
            ]
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def disconnect(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for task in self._raw_dispatch_tasks:
            task.cancel()
        if self._raw_dispatch_tasks:
            await asyncio.gather(*self._raw_dispatch_tasks, return_exceptions=True)
        self._raw_dispatch_tasks = []
        if self._candle_dispatch_task:
            self._candle_dispatch_task.cancel()
            await asyncio.gather(
                self._candle_dispatch_task,
                return_exceptions=True,
            )
            self._candle_dispatch_task = None
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

    async def _dispatch_raw_messages(
        self,
        queue: asyncio.Queue[tuple[RawMarketEvent, dict]],
        shard: int,
    ) -> None:
        while not self._stop.is_set():
            try:
                first = await queue.get()
            except asyncio.CancelledError:
                raise
            cohort = self._drain_raw_cohort(queue, first)
            started = monotonic()
            events = [item[0] for item in cohort]
            try:
                if self.on_raw_events is not None:
                    await self._emit_raw_events(events)
                else:
                    await asyncio.gather(
                        *(self._emit_raw_event(event) for event in events)
                    )
                cohort_ms = round((monotonic() - started) * 1000, 2)
                for raw_event, message in cohort:
                    candle = normalize_kline(
                        message,
                        received_at=raw_event.received_at,
                    )
                    if candle is not None:
                        await self._candle_queue.put(candle)
                        logger.info(
                            "Closed candle queued after durable raw ingestion",
                            event_type="MARKET_CANDLE_QUEUED",
                            metadata={
                                "symbol": candle.symbol,
                                "timeframe": candle.timeframe,
                                "closed_at": candle.closed_at.isoformat(),
                                "candle_queue_depth": self._candle_queue.qsize(),
                                "raw_queue_depth": self.pending_raw_events,
                                "raw_ingest_ms": cohort_ms,
                                "raw_cohort_size": len(cohort),
                                "shard": shard,
                                "batch_persistence": self.on_raw_events is not None,
                            },
                        )
                if len(cohort) > 1:
                    logger.info(
                        "Raw market cohort persisted",
                        event_type="MARKET_RAW_COHORT_PERSISTED",
                        metadata={
                            "cohort_size": len(cohort),
                            "raw_queue_depth": self.pending_raw_events,
                            "duration_ms": cohort_ms,
                            "shard": shard,
                            "batch_persistence": self.on_raw_events is not None,
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Raw market cohort ingestion failed; normalized candles suppressed",
                    event_type="MARKET_RAW_INGESTION_FAILED",
                    metadata={
                        "symbols": sorted(
                            {
                                event.symbol
                                for event in events
                                if event.symbol is not None
                            }
                        ),
                        "cohort_size": len(cohort),
                        "raw_queue_depth": self.pending_raw_events,
                        "duration_ms": round((monotonic() - started) * 1000, 2),
                        "error_type": type(exc).__name__,
                        "shard": shard,
                        "batch_persistence": self.on_raw_events is not None,
                    },
                )
            finally:
                for _ in cohort:
                    queue.task_done()

    async def _dispatch_candles(self) -> None:
        while not self._stop.is_set():
            try:
                candle = await self._candle_queue.get()
            except asyncio.CancelledError:
                raise
            started = monotonic()
            queue_depth_at_start = self._candle_queue.qsize()
            logger.info(
                "Closed candle dispatch started",
                event_type="MARKET_CANDLE_DISPATCH_STARTED",
                metadata={
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "closed_at": candle.closed_at.isoformat(),
                    "queue_depth": queue_depth_at_start,
                    "raw_queue_depth": self.pending_raw_events,
                },
            )
            try:
                await self._emit_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Closed candle downstream dispatch failed",
                    event_type="MARKET_CANDLE_DISPATCH_FAILED",
                    metadata={
                        "symbol": candle.symbol,
                        "timeframe": candle.timeframe,
                        "closed_at": candle.closed_at.isoformat(),
                        "queue_depth": self._candle_queue.qsize(),
                        "raw_queue_depth": self.pending_raw_events,
                        "duration_ms": round((monotonic() - started) * 1000, 2),
                        "error_type": type(exc).__name__,
                    },
                )
            else:
                logger.info(
                    "Closed candle dispatch completed",
                    event_type="MARKET_CANDLE_DISPATCH_COMPLETED",
                    metadata={
                        "symbol": candle.symbol,
                        "timeframe": candle.timeframe,
                        "closed_at": candle.closed_at.isoformat(),
                        "queue_depth": self._candle_queue.qsize(),
                        "raw_queue_depth": self.pending_raw_events,
                        "duration_ms": round((monotonic() - started) * 1000, 2),
                    },
                )
            finally:
                self._candle_queue.task_done()

    async def _run(self) -> None:
        import websockets

        retries = 0
        while not self._stop.is_set() and retries <= self._max_retries:
            try:
                async with websockets.connect(
                    self._stream_url(),
                    ping_interval=None,
                ) as ws:
                    retries = 0
                    self.connected = True
                    await self._emit_status(
                        "MARKET_CONNECTED", {"exchange": self.exchange_name}
                    )
                    async for raw in ws:
                        message = json.loads(raw)
                        await self._queue_raw_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                retries += 1
                backoff = min(2 ** retries, 60)
                logger.warning(
                    f"Binance WS error, reconnecting in {backoff}s",
                    event_type="MARKET_DISCONNECTED",
                    metadata={
                        "retries": retries,
                        "error": str(exc),
                        "raw_queue_depth": self.pending_raw_events,
                        "candle_queue_depth": self.pending_candles,
                    },
                )
                await self._emit_status(
                    "MARKET_DISCONNECTED",
                    {"exchange": self.exchange_name, "error": str(exc)},
                )
                await asyncio.sleep(backoff)

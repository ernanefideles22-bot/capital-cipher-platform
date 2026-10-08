"""Binance WebSocket dispatch isolation tests."""

from __future__ import annotations

import asyncio

from app.market_data.adapters.binance import BinanceMarketDataAdapter, normalize_kline


def _payload(
    symbol: str,
    close: str,
    close_time_ms: int,
    *,
    closed: bool,
) -> dict:
    return {
        "e": "kline",
        "k": {
            "s": symbol,
            "i": "5m",
            "x": closed,
            "o": "100",
            "h": "102",
            "l": "99",
            "c": close,
            "v": "10",
            "T": close_time_ms,
        },
    }


def _closed_payload(symbol: str, close: str, close_time_ms: int) -> dict:
    return _payload(
        symbol,
        close,
        close_time_ms,
        closed=True,
    )


async def test_closed_candle_dispatch_queue_isolated_from_slow_handler():
    adapter = BinanceMarketDataAdapter(candle_queue_size=4)
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_handler(candle):
        seen.append(candle.symbol)
        started.set()
        await release.wait()

    adapter.on_candle = slow_handler
    dispatch_task = asyncio.create_task(adapter._dispatch_candles())
    first = normalize_kline(_closed_payload("BTCUSDT", "101", 1767268800000))
    second = normalize_kline(_closed_payload("ETHUSDT", "102", 1767269100000))
    assert first is not None
    assert second is not None

    await adapter._candle_queue.put(first)
    await asyncio.wait_for(started.wait(), timeout=1)

    # The downstream handler is still blocked on the first candle, but ingestion
    # can enqueue the next closed candle without waiting for that decision cycle.
    adapter._candle_queue.put_nowait(second)
    assert adapter.pending_candles == 1

    release.set()
    await asyncio.wait_for(adapter._candle_queue.join(), timeout=1)
    assert seen == ["BTCUSDT", "ETHUSDT"]
    assert adapter.pending_candles == 0

    dispatch_task.cancel()
    await asyncio.gather(dispatch_task, return_exceptions=True)


async def test_raw_ingestion_is_parallel_across_symbols_but_bounded():
    adapter = BinanceMarketDataAdapter(
        raw_queue_size=4,
        raw_queue_shards=8,
    )
    assert adapter._raw_queue_index("BTCUSDT") != adapter._raw_queue_index(
        "ETHUSDT"
    )

    btc_started = asyncio.Event()
    release_btc = asyncio.Event()
    eth_seen = asyncio.Event()

    async def raw_handler(event):
        if event.symbol == "BTCUSDT":
            btc_started.set()
            await release_btc.wait()
        elif event.symbol == "ETHUSDT":
            eth_seen.set()

    adapter.on_raw_event = raw_handler
    raw_tasks = [
        asyncio.create_task(adapter._dispatch_raw_messages(queue, shard))
        for shard, queue in enumerate(adapter._raw_queues)
    ]

    await adapter._queue_raw_message(
        _payload("BTCUSDT", "101", 1767268800000, closed=False)
    )
    await asyncio.wait_for(btc_started.wait(), timeout=1)

    # A slow durable write for BTC must not block ETH frame persistence or the
    # WebSocket reader because each symbol is deterministically sharded.
    await adapter._queue_raw_message(
        _payload("ETHUSDT", "102", 1767268801000, closed=False)
    )
    await asyncio.wait_for(eth_seen.wait(), timeout=1)

    release_btc.set()
    await asyncio.wait_for(
        asyncio.gather(*(queue.join() for queue in adapter._raw_queues)),
        timeout=1,
    )
    assert adapter.pending_raw_events == 0

    for task in raw_tasks:
        task.cancel()
    await asyncio.gather(*raw_tasks, return_exceptions=True)


async def test_closed_candle_is_suppressed_when_raw_persistence_fails():
    adapter = BinanceMarketDataAdapter(raw_queue_size=2, raw_queue_shards=1)

    async def failing_raw_handler(_event):
        raise RuntimeError("durable raw store unavailable")

    adapter.on_raw_event = failing_raw_handler
    raw_task = asyncio.create_task(
        adapter._dispatch_raw_messages(adapter._raw_queues[0], 0)
    )

    await adapter._queue_raw_message(
        _closed_payload("BTCUSDT", "101", 1767268800000)
    )
    await asyncio.wait_for(adapter._raw_queues[0].join(), timeout=1)

    assert adapter.pending_candles == 0
    raw_task.cancel()
    await asyncio.gather(raw_task, return_exceptions=True)


def test_binance_queues_must_be_bounded_positive():
    for kwargs in (
        {"candle_queue_size": 0},
        {"raw_queue_size": 0},
        {"raw_queue_shards": 0},
    ):
        try:
            BinanceMarketDataAdapter(**kwargs)
        except ValueError as exc:
            assert "positive" in str(exc)
        else:
            raise AssertionError("zero-sized queue configuration must be rejected")

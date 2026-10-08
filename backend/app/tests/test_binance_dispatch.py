"""Binance WebSocket dispatch isolation tests."""

from __future__ import annotations

import asyncio

from app.market_data.adapters.binance import BinanceMarketDataAdapter, normalize_kline


def _closed_payload(symbol: str, close: str, close_time_ms: int) -> dict:
    return {
        "e": "kline",
        "k": {
            "s": symbol,
            "i": "5m",
            "x": True,
            "o": "100",
            "h": "102",
            "l": "99",
            "c": close,
            "v": "10",
            "T": close_time_ms,
        },
    }


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


def test_binance_candle_queue_must_be_bounded_positive():
    try:
        BinanceMarketDataAdapter(candle_queue_size=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("zero-sized candle queue must be rejected")

"""Verify closed candles from Binance's public combined WebSocket stream."""

from __future__ import annotations

import asyncio
import json

import websockets

from app.market_data.adapters.binance import (
    BinanceMarketDataAdapter,
    normalize_kline,
)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TIMEFRAME = "1m"
TIMEOUT_SECONDS = 85


async def smoke() -> dict:
    adapter = BinanceMarketDataAdapter()
    for symbol in SYMBOLS:
        await adapter.subscribe_candles(symbol, TIMEFRAME)

    closed: dict[str, str] = {}
    async with websockets.connect(
        adapter._stream_url(),
        ping_interval=20,
        open_timeout=10,
    ) as websocket:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async for raw in websocket:
                candle = normalize_kline(json.loads(raw))
                if candle is None:
                    continue
                closed[candle.symbol] = candle.closed_at.isoformat()
                if len(closed) == len(SYMBOLS):
                    break

    return {
        "mode": "READ_ONLY",
        "endpoint": "BINANCE_COMBINED_PUBLIC_STREAM",
        "timeframe": TIMEFRAME,
        "closed": closed,
    }


def main() -> None:
    print(json.dumps(asyncio.run(smoke()), sort_keys=True))


if __name__ == "__main__":
    main()

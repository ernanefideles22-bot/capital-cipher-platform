"""Bybit TESTNET public-feed safety and heartbeat tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.market_data.adapters.bybit import (
    BYBIT_MAINNET_WS_URL,
    BYBIT_TESTNET_WS_URL,
    BybitMarketDataAdapter,
)
from app.schemas.common import Exchange


class _FakeWebSocket:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._messages = [json.dumps(item) for item in (messages or [])]

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        raise RuntimeError("simulated websocket disconnect")


class _FakeConnection:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _TestAdapter(BybitMarketDataAdapter):
    def __init__(self, websocket: _FakeWebSocket, **kwargs) -> None:
        super().__init__(**kwargs)
        self.websocket = websocket

    def _connect(self):
        return _FakeConnection(self.websocket)


def test_bybit_market_adapter_accepts_only_official_linear_endpoints():
    assert BybitMarketDataAdapter(ws_url=BYBIT_MAINNET_WS_URL).ws_url == (
        BYBIT_MAINNET_WS_URL
    )
    assert BybitMarketDataAdapter(ws_url=BYBIT_TESTNET_WS_URL).ws_url == (
        BYBIT_TESTNET_WS_URL
    )
    with pytest.raises(ValueError, match="official linear endpoint"):
        BybitMarketDataAdapter(ws_url="wss://example.invalid/bybit")


@pytest.mark.asyncio
async def test_bybit_heartbeat_uses_documented_json_ping():
    websocket = _FakeWebSocket()
    adapter = _TestAdapter(
        websocket,
        ws_url=BYBIT_TESTNET_WS_URL,
        heartbeat_seconds=0.001,
    )

    task = asyncio.create_task(adapter._heartbeat(websocket))
    await asyncio.sleep(0.005)
    adapter._stop.set()
    await task

    assert websocket.sent
    assert all(message == {"op": "ping"} for message in websocket.sent)


@pytest.mark.asyncio
async def test_bybit_testnet_feed_subscribes_and_emits_only_closed_candle():
    websocket = _FakeWebSocket(
        [
            {
                "success": True,
                "ret_msg": "",
                "op": "subscribe",
            },
            {
                "topic": "kline.15.BTCUSDT",
                "ts": 1_767_268_800_123,
                "data": [
                    {
                        "end": 1_767_268_800_000,
                        "confirm": False,
                        "open": "100000",
                        "high": "101000",
                        "low": "99500",
                        "close": "100500",
                        "volume": "10",
                    }
                ],
            },
            {
                "topic": "kline.15.BTCUSDT",
                "ts": 1_767_268_800_456,
                "data": [
                    {
                        "end": 1_767_268_800_000,
                        "confirm": True,
                        "open": "100000",
                        "high": "101000",
                        "low": "99500",
                        "close": "100700",
                        "volume": "12",
                    }
                ],
            },
        ]
    )
    adapter = _TestAdapter(
        websocket,
        ws_url=BYBIT_TESTNET_WS_URL,
        max_retries=0,
        heartbeat_seconds=20,
    )
    candles = []
    raw_events = []
    statuses = []

    async def on_candle(candle) -> None:
        candles.append(candle)

    async def on_raw_event(event) -> None:
        raw_events.append(event)

    async def on_status(event_type: str, payload: dict) -> None:
        statuses.append((event_type, payload))

    adapter.on_candle = on_candle
    adapter.on_raw_event = on_raw_event
    adapter.on_status = on_status
    await adapter.subscribe_candles("BTCUSDT", "15m")
    await adapter._run()

    assert websocket.sent[0] == {
        "op": "subscribe",
        "args": ["kline.15.BTCUSDT"],
    }
    assert len(candles) == 1
    assert candles[0].exchange == Exchange.BYBIT
    assert candles[0].symbol == "BTCUSDT"
    assert candles[0].close == 100700.0
    assert len(raw_events) == 2
    assert statuses[0][0] == "MARKET_CONNECTED"
    assert statuses[-1][0] == "MARKET_DISCONNECTED"
    assert statuses[-1][1]["retry_exhausted"] is True

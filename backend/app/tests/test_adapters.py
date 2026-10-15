"""Adapter normalization tests (docs/33)."""

from __future__ import annotations

from app.market_data.adapters.binance import (
    BinanceMarketDataAdapter,
    build_raw_kline_event as build_binance_raw,
)
from app.market_data.adapters.binance import normalize_kline as binance_normalize
from app.market_data.adapters.bybit import build_raw_kline_event as build_bybit_raw
from app.market_data.adapters.bybit import normalize_kline as bybit_normalize


def test_binance_closed_kline_normalized():
    payload = {
        "e": "kline",
        "k": {
            "s": "BTCUSDT", "i": "15m", "x": True,
            "o": "100000.0", "h": "101000.0", "l": "99500.0", "c": "100700.0",
            "v": "1234.56", "T": 1767268800000,
        },
    }
    candle = binance_normalize(payload)
    assert candle is not None
    assert candle.symbol == "BTCUSDT"
    assert candle.exchange.value == "BINANCE"
    assert candle.close == 100700.0


def test_binance_raw_payload_is_preserved_before_normalization():
    payload = {
        "e": "kline",
        "E": 1767268800123,
        "k": {"s": "BTCUSDT", "i": "15m", "x": True, "T": 1767268800000},
    }
    event = build_binance_raw(payload)
    assert event is not None
    assert event.payload == payload
    assert event.symbol == "BTCUSDT"
    assert event.schema_version == "1.0.0"
    assert len(event.payload_sha256) == 64


def test_binance_combined_payload_is_preserved_and_normalized():
    payload = {
        "stream": "btcusdt@kline_15m",
        "data": {
            "e": "kline",
            "E": 1767268800123,
            "k": {
                "s": "BTCUSDT",
                "i": "15m",
                "x": True,
                "o": "100000.0",
                "h": "101000.0",
                "l": "99500.0",
                "c": "100700.0",
                "v": "1234.56",
                "T": 1767268800000,
            },
        },
    }

    event = build_binance_raw(payload)
    candle = binance_normalize(payload)

    assert event is not None
    assert event.payload == payload
    assert event.symbol == "BTCUSDT"
    assert candle is not None
    assert candle.symbol == "BTCUSDT"
    assert candle.close == 100700.0


async def test_binance_multiple_symbols_use_official_combined_stream_url():
    adapter = BinanceMarketDataAdapter()
    await adapter.subscribe_candles("SOLUSDT", "15m")
    await adapter.subscribe_candles("BTCUSDT", "15m")
    await adapter.subscribe_candles("ETHUSDT", "15m")

    assert adapter._stream_url() == (
        "wss://stream.binance.com:9443/stream?streams="
        "btcusdt@kline_15m/ethusdt@kline_15m/solusdt@kline_15m"
    )


def test_binance_open_kline_ignored():
    payload = {"k": {"s": "BTCUSDT", "i": "15m", "x": False}}
    assert binance_normalize(payload) is None


def test_bybit_confirmed_kline_normalized():
    message = {
        "topic": "kline.15.BTCUSDT",
        "data": [
            {
                "start": 1767267900000, "end": 1767268800000, "confirm": True,
                "open": "100000", "high": "101000", "low": "99500",
                "close": "100700", "volume": "1234.56",
            }
        ],
    }
    candles = bybit_normalize(message)
    assert len(candles) == 1
    assert candles[0].exchange.value == "BYBIT"
    assert candles[0].timeframe == "15m"


def test_bybit_raw_event_has_deterministic_identity():
    payload = {
        "topic": "kline.15.BTCUSDT",
        "ts": 1767268800123,
        "data": [{"end": 1767268800000}],
    }
    first = build_bybit_raw(payload)
    second = build_bybit_raw(payload)
    assert first is not None
    assert second is not None
    assert first.event_id == second.event_id
    assert first.payload_sha256 == second.payload_sha256


def test_bybit_unconfirmed_ignored():
    message = {
        "topic": "kline.15.BTCUSDT",
        "data": [{"confirm": False, "open": "1", "high": "2", "low": "0.5", "close": "1.5",
                   "volume": "1", "end": 1767268800000}],
    }
    assert bybit_normalize(message) == []


def test_bybit_non_kline_topic_ignored():
    assert bybit_normalize({"topic": "tickers.BTCUSDT", "data": []}) == []

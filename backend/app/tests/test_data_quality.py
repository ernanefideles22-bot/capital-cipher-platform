"""Data quality tests (docs/32)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market_data.data_quality import evaluate_candles, validate_raw_candle
from app.tests.conftest import make_candle, make_series


def test_valid_series_scores_high():
    candles = make_series([100 + i * 0.1 for i in range(50)])
    report = evaluate_candles(candles, timeframe="15m")
    assert report.data_quality_score >= 80
    assert report.status in ("VALID", "WARNING")


def test_empty_series_invalid():
    report = evaluate_candles([], timeframe="15m")
    assert report.data_quality_score == 0
    assert report.status == "INVALID"


def test_out_of_order_candles_penalized():
    candles = make_series([100.0, 101.0, 102.0])
    candles[0], candles[2] = candles[2], candles[0]
    report = evaluate_candles(candles, timeframe="15m")
    assert "CANDLES_OUT_OF_ORDER" in report.errors
    assert report.status == "INVALID"


def test_gap_detected():
    now = datetime.now(timezone.utc)
    candles = [
        make_candle(100.0, closed_at=now - timedelta(minutes=90)),
        make_candle(101.0, closed_at=now - timedelta(minutes=75)),
        make_candle(102.0, closed_at=now),  # 75-minute gap
    ]
    report = evaluate_candles(candles, timeframe="15m")
    assert "GAP_DETECTED" in report.warnings


def test_stale_data_detected():
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    candles = make_series([100.0, 101.0, 102.0])
    candles = [c.model_copy(update={"closed_at": old + timedelta(minutes=15 * i)}) for i, c in enumerate(candles)]
    report = evaluate_candles(candles, timeframe="15m")
    assert "STALE_DATA" in report.warnings


def test_invalid_raw_candle_rejected():
    candle, errors = validate_raw_candle(
        {
            "exchange": "BINANCE",
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "open": 100,
            "high": 90,
            "low": 95,
            "close": 99,
            "volume": 10,
            "closed_at": "2026-07-01T12:00:00Z",
        }
    )
    assert candle is None
    assert errors

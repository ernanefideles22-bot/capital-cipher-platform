"""Indicator unit tests (docs/22)."""

from __future__ import annotations

from app.agents import indicators
from app.tests.conftest import make_series


def test_ema_seeds_with_sma():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = indicators.ema(values, 3)
    assert result[0] == 2.0  # SMA of first 3
    assert len(result) == 3


def test_ema_insufficient_data():
    assert indicators.ema([1.0, 2.0], 5) == []


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 20)]
    assert indicators.rsi(closes) == 100.0


def test_rsi_range():
    closes = [100 + ((-1) ** i) * (i % 5) for i in range(40)]
    value = indicators.rsi([float(c) for c in closes])
    assert 0 <= value <= 100


def test_atr_positive():
    candles = make_series([100 + i * 0.5 for i in range(30)])
    assert indicators.atr(candles) > 0


def test_vwap_weighted():
    candles = make_series([100.0, 200.0])
    vwap = indicators.vwap(candles)
    assert vwap is not None
    assert 100 < vwap < 200


def test_volume_ratio():
    candles = make_series([100.0] * 25)
    # all volumes equal -> ratio 1.0
    assert abs(indicators.volume_ratio(candles) - 1.0) < 1e-9


def test_macd_returns_triplet():
    closes = [100 + i * 0.3 for i in range(60)]
    result = indicators.macd(closes)
    assert result is not None
    macd_line, signal_line, histogram = result
    assert abs((macd_line - signal_line) - histogram) < 1e-9

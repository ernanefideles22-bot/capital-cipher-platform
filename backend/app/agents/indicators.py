"""Technical indicators (docs/05-agentes-fase-1.md — Quant Agent).

Pure-python implementations, computed only with data available at decision
time (no lookahead — docs/17, docs/32).
"""

from __future__ import annotations

from app.schemas.market import Candle


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average. First value seeds with SMA of `period`."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    result = [seed]
    for value in values[period:]:
        result.append(value * k + result[-1] * (1 - k))
    return result


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for prev, curr in zip(closes, closes[1:]):
        change = curr - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Average True Range (Wilder smoothing)."""
    if len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for prev, curr in zip(candles, candles[1:]):
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        true_ranges.append(tr)
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def vwap(candles: list[Candle]) -> float | None:
    """Volume-weighted average price over the provided window."""
    total_volume = sum(c.volume for c in candles)
    if total_volume == 0:
        return None
    return sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles) / total_volume


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9
) -> tuple[float, float, float] | None:
    """Returns (macd_line, signal_line, histogram) for the latest close."""
    if len(closes) < slow + signal_period:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    # Align series tails.
    size = min(len(ema_fast), len(ema_slow))
    macd_line_series = [f - s for f, s in zip(ema_fast[-size:], ema_slow[-size:])]
    signal_series = ema(macd_line_series, signal_period)
    if not signal_series:
        return None
    macd_value = macd_line_series[-1]
    signal_value = signal_series[-1]
    return macd_value, signal_value, macd_value - signal_value


def volume_ratio(candles: list[Candle], period: int = 20) -> float | None:
    """Latest volume relative to the average of the previous `period` candles."""
    if len(candles) < period + 1:
        return None
    recent = candles[-(period + 1):-1]
    avg = sum(c.volume for c in recent) / period
    if avg == 0:
        return None
    return candles[-1].volume / avg

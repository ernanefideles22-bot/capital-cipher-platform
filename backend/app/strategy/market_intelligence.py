"""Read-only, point-in-time OHLCV interpretation for strategy research.

This module emits observations, not orders. Neither agents nor this module
are authorized to place trades or change risk controls.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Sequence

from app.schemas.market import Candle


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timeframe: str
    as_of: datetime
    last_closed_at: datetime
    regime: str
    ema20: float
    ema50: float
    atr14: float
    atr_percent: float
    relative_volume: float
    candle_pattern: str
    breakout: str


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    current = values[0]
    series = [current]
    for value in values[1:]:
        current += alpha * (value - current)
        series.append(current)
    return series


def analyze_closed_candles(
    candles: Sequence[Candle], *, as_of: datetime
) -> MarketSnapshot:
    """Interpret ONLY candles fully known by the decision cutoff.

    The input must be chronological, unique and homogeneous. Reject future
    closes/receipt timestamps instead of silently trimming them.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if len(candles) < 60:
        raise ValueError("At least 60 closed candles are required")
    symbol, timeframe, exchange = (
        candles[0].symbol, candles[0].timeframe, candles[0].exchange
    )
    previous = None
    cutoff = as_of.astimezone(timezone.utc)
    for bar in candles:
        if (bar.symbol, bar.timeframe, bar.exchange) != (
            symbol, timeframe, exchange
        ):
            raise ValueError("Mixed symbols, timeframes or exchanges")
        close_time = bar.closed_at.astimezone(timezone.utc)
        received_time = bar.received_at.astimezone(timezone.utc)
        if close_time > cutoff or received_time > cutoff:
            raise ValueError("Future/unavailable candle: lookahead forbidden")
        if previous is not None and close_time <= previous:
            raise ValueError("Candles must be unique and strictly increasing")
        if not all(
            isfinite(v) for v in (bar.open, bar.high, bar.low, bar.close, bar.volume)
        ):
            raise ValueError("Non-finite market data")
        previous = close_time

    closes = [bar.close for bar in candles]
    fast = _ema(closes, 20)
    slow = _ema(closes, 50)
    last = candles[-1]
    true_ranges = []
    for idx in range(1, len(candles)):
        bar, prior = candles[idx], candles[idx - 1]
        true_ranges.append(max(
            bar.high - bar.low,
            abs(bar.high - prior.close),
            abs(bar.low - prior.close),
        ))
    atr = sum(true_ranges[-14:]) / 14
    atr_pct = 100.0 * atr / last.close
    baseline_volume = sum(bar.volume for bar in candles[-21:-1]) / 20
    relative_volume = (
        last.volume / baseline_volume if baseline_volume > 0 else 0.0
    )
    body = abs(last.close - last.open)
    lower_wick = min(last.open, last.close) - last.low
    upper_wick = last.high - max(last.open, last.close)
    pattern = "NONE"
    if body > 0 and lower_wick >= 2 * body and lower_wick > upper_wick:
        pattern = "LOWER_WICK_REJECTION"
    elif body > 0 and upper_wick >= 2 * body and upper_wick > lower_wick:
        pattern = "UPPER_WICK_REJECTION"
    elif last.high > last.low and body / (last.high - last.low) <= 0.1:
        pattern = "DOJI"

    prior_high = max(bar.high for bar in candles[-21:-1])
    prior_low = min(bar.low for bar in candles[-21:-1])
    breakout = "NONE"
    if relative_volume >= 1.2:
        if last.close > prior_high:
            breakout = "UP"
        elif last.close < prior_low:
            breakout = "DOWN"

    # Regime thresholds are descriptive baselines for research, NOT a claim
    # of economic edge and NOT execution permission.
    separation = (fast[-1] - slow[-1]) / last.close
    fast_slope = fast[-1] - fast[-6]
    if atr_pct > 3.0:
        regime = "HIGH_VOLATILITY"
    elif separation > 0.001 and fast_slope > 0:
        regime = "UPTREND"
    elif separation < -0.001 and fast_slope < 0:
        regime = "DOWNTREND"
    else:
        regime = "RANGE_OR_TRANSITION"
    return MarketSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        as_of=cutoff,
        last_closed_at=previous,
        regime=regime,
        ema20=round(fast[-1], 8),
        ema50=round(slow[-1], 8),
        atr14=round(atr, 8),
        atr_percent=round(atr_pct, 6),
        relative_volume=round(relative_volume, 6),
        candle_pattern=pattern,
        breakout=breakout,
    )

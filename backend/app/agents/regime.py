"""Deterministic market-regime classification shared by runtime and shadow research."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents import indicators
from app.schemas.common import MarketRegime, Signal
from app.schemas.market import Candle

MIN_REGIME_CANDLES = 60


@dataclass(frozen=True)
class RegimeClassification:
    regime: MarketRegime
    signal: Signal
    confidence: int
    reason: str
    volatility_state: str
    ema21_slope: float
    atr_percent: float


def classify_market_regime(candles: list[Candle]) -> RegimeClassification:
    """Classify regime using only candles supplied by the caller.

    Callers performing historical evaluation must pass only candles whose
    closed_at is not after the forecast timestamp. This keeps the classifier
    free of look-ahead bias.
    """
    if len(candles) < MIN_REGIME_CANDLES:
        return RegimeClassification(
            regime=MarketRegime.UNDEFINED,
            signal=Signal.WAIT,
            confidence=0,
            reason=(
                f"Insufficient candles ({len(candles)}/{MIN_REGIME_CANDLES}) "
                "to classify regime"
            ),
            volatility_state="UNKNOWN",
            ema21_slope=0.0,
            atr_percent=0.0,
        )

    sample = candles[-200:]
    closes = [c.close for c in sample]
    price = closes[-1]
    ema21 = indicators.ema(closes, 21)
    ema50 = indicators.ema(closes, 50)
    atr_value = indicators.atr(sample)
    slope21 = (
        (ema21[-1] - ema21[-10]) / ema21[-10]
        if len(ema21) >= 10 and ema21[-10]
        else 0.0
    )
    atr_percent = (atr_value / price * 100) if atr_value and price else 0.0

    window = sample[-30:]
    half = len(window) // 2
    first_half_high = max(c.high for c in window[:half])
    second_half_high = max(c.high for c in window[half:])
    first_half_low = min(c.low for c in window[:half])
    second_half_low = min(c.low for c in window[half:])
    higher_structure = (
        second_half_high > first_half_high and second_half_low > first_half_low
    )
    lower_structure = (
        second_half_high < first_half_high and second_half_low < first_half_low
    )

    volatility_state = "NORMAL"
    if atr_percent > 3.0:
        regime = MarketRegime.HIGH_VOLATILITY
        volatility_state = "HIGH"
        signal, confidence = Signal.WAIT, 70
        reason = f"ATR {atr_percent:.2f}% of price indicates extreme volatility"
    elif atr_percent < 0.15:
        regime = MarketRegime.LOW_VOLATILITY
        volatility_state = "LOW"
        signal, confidence = Signal.HOLD, 60
        reason = f"ATR {atr_percent:.2f}% of price indicates very low volatility"
    elif slope21 > 0.001 and higher_structure and price > ema50[-1]:
        regime = MarketRegime.BULL_TREND
        signal, confidence = Signal.BUY, 75
        reason = "Higher highs and higher lows with positive EMA slope"
    elif slope21 < -0.001 and lower_structure and price < ema50[-1]:
        regime = MarketRegime.BEAR_TREND
        signal, confidence = Signal.SELL, 75
        reason = "Lower highs and lower lows with negative EMA slope"
    elif abs(slope21) <= 0.001:
        regime = MarketRegime.RANGE
        signal, confidence = Signal.HOLD, 60
        reason = "Flat EMA slope and no clear structure: range regime"
    else:
        regime = MarketRegime.UNDEFINED
        signal, confidence = Signal.WAIT, 40
        reason = "Mixed structure signals: regime undefined"

    return RegimeClassification(
        regime=regime,
        signal=signal,
        confidence=confidence,
        reason=reason,
        volatility_state=volatility_state,
        ema21_slope=slope21,
        atr_percent=atr_percent,
    )

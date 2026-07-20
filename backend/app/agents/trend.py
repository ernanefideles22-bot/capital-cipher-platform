"""Trend Agent (docs/05 §3): classifies market regime."""

from __future__ import annotations

from app.agents import indicators
from app.agents.base import BaseAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, MarketRegime, Signal

MIN_CANDLES = 60


class TrendAgent(BaseAgent):
    name = "TrendAgent"
    description = "Classifies the current market regime"
    required_inputs = ("candles.ohlcv",)
    capabilities = ("market-regime", "trend-structure", "volatility-state")
    decision_role = "PRIMARY"
    critical = True

    def __init__(self, store: CandleStore) -> None:
        super().__init__()
        self._store = store

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        exchange = agent_input.market_context.get("exchange", "BINANCE")
        candles = self._store.get(exchange, agent_input.symbol, agent_input.timeframe, limit=200)
        if len(candles) < MIN_CANDLES:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                f"Insufficient candles ({len(candles)}/{MIN_CANDLES}) to classify regime",
                evidence={"market_regime": MarketRegime.UNDEFINED.value},
                warnings=["REGIME_UNCLEAR"],
            )

        closes = [c.close for c in candles]
        price = closes[-1]
        ema21 = indicators.ema(closes, 21)
        ema50 = indicators.ema(closes, 50)
        atr_value = indicators.atr(candles)

        # EMA slope over the last 10 points.
        slope21 = (ema21[-1] - ema21[-10]) / ema21[-10] if len(ema21) >= 10 else 0.0
        atr_percent = (atr_value / price * 100) if atr_value else 0.0

        # Structure: higher highs / lower lows over recent window.
        window = candles[-30:]
        half = len(window) // 2
        first_half_high = max(c.high for c in window[:half])
        second_half_high = max(c.high for c in window[half:])
        first_half_low = min(c.low for c in window[:half])
        second_half_low = min(c.low for c in window[half:])
        higher_structure = second_half_high > first_half_high and second_half_low > first_half_low
        lower_structure = second_half_high < first_half_high and second_half_low < first_half_low

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

        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            reason,
            evidence={
                "market_regime": regime.value,
                "volatility_state": volatility_state,
                "ema21_slope": round(slope21, 6),
                "atr_percent": round(atr_percent, 4),
            },
            warnings=["REGIME_UNCLEAR"] if regime == MarketRegime.UNDEFINED else [],
        )

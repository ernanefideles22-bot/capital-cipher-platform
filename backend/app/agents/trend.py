"""Trend Agent (docs/05 §3): classifies market regime."""

from __future__ import annotations

from app.agents.base import BaseAgent
from app.agents.regime import MIN_REGIME_CANDLES, classify_market_regime
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, MarketRegime


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
        candles = self._store.get(
            exchange,
            agent_input.symbol,
            agent_input.timeframe,
            limit=200,
        )
        classification = classify_market_regime(candles)
        warnings = (
            ["REGIME_UNCLEAR"]
            if classification.regime == MarketRegime.UNDEFINED
            else []
        )
        return self._output(
            AgentStatus.COMPLETED,
            classification.signal,
            classification.confidence,
            classification.reason,
            evidence={
                "market_regime": classification.regime.value,
                "volatility_state": classification.volatility_state,
                "ema21_slope": round(classification.ema21_slope, 6),
                "atr_percent": round(classification.atr_percent, 4),
                "minimum_regime_candles": MIN_REGIME_CANDLES,
            },
            warnings=warnings,
        )

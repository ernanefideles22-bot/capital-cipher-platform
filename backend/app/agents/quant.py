"""Quant Agent (docs/05 §2, docs/11).

Quantitative technical analysis: EMA 9/21/50, RSI, ATR, VWAP, MACD, volume ratio.
"""

from __future__ import annotations

from app.agents import indicators
from app.agents.base import BaseAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal

MIN_CANDLES = 60


class QuantAgent(BaseAgent):
    name = "QuantAgent"
    description = "Performs quantitative technical analysis"
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
                f"Insufficient candles ({len(candles)}/{MIN_CANDLES}) for reliable indicators",
                warnings=["DATA_QUALITY_ISSUE"],
            )

        closes = [c.close for c in candles]
        price = closes[-1]
        ema9_series = indicators.ema(closes, 9)
        ema21_series = indicators.ema(closes, 21)
        ema50_series = indicators.ema(closes, 50)
        ema9, ema21, ema50 = ema9_series[-1], ema21_series[-1], ema50_series[-1]
        rsi_value = indicators.rsi(closes)
        atr_value = indicators.atr(candles)
        vwap_value = indicators.vwap(candles[-50:])
        macd_result = indicators.macd(closes)
        vol_ratio = indicators.volume_ratio(candles)

        warnings: list[str] = []
        bullish_points = 0
        bearish_points = 0

        if ema9 > ema21 > ema50:
            bullish_points += 2
        elif ema9 < ema21 < ema50:
            bearish_points += 2
        if vwap_value is not None:
            if price > vwap_value:
                bullish_points += 1
            else:
                bearish_points += 1
        if macd_result is not None:
            _, _, histogram = macd_result
            if histogram > 0:
                bullish_points += 1
            else:
                bearish_points += 1
        if rsi_value is not None:
            if rsi_value >= 70:
                warnings.append("RSI_OVERBOUGHT")
                bullish_points -= 1
            elif rsi_value <= 30:
                warnings.append("RSI_OVERSOLD")
                bearish_points -= 1
        if vol_ratio is not None and vol_ratio < 0.5:
            warnings.append("LOW_VOLUME")

        score = bullish_points - bearish_points
        if score >= 2:
            signal = Signal.BUY
        elif score <= -2:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD

        alignment = abs(score)
        confidence = min(95, 40 + alignment * 15) if signal != Signal.HOLD else 50
        if warnings:
            confidence = max(0, confidence - 10 * len(warnings))

        reason_parts = [
            f"EMA alignment {'bullish' if ema9 > ema21 else 'bearish' if ema9 < ema21 else 'flat'}",
            f"price {'above' if vwap_value and price > vwap_value else 'below'} VWAP",
        ]
        if rsi_value is not None:
            reason_parts.append(f"RSI {rsi_value:.1f}")

        evidence = {
            "ema_9": round(ema9, 4),
            "ema_21": round(ema21, 4),
            "ema_50": round(ema50, 4),
            "rsi": round(rsi_value, 2) if rsi_value is not None else None,
            "atr": round(atr_value, 4) if atr_value is not None else None,
            "vwap": round(vwap_value, 4) if vwap_value is not None else None,
            "macd_histogram": round(macd_result[2], 6) if macd_result else None,
            "volume_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
            "price": price,
        }
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            ", ".join(reason_parts),
            evidence=evidence,
            warnings=warnings,
        )

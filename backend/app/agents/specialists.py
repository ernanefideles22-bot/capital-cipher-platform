"""Twelve deterministic shadow specialists for the Month 5 PAPER runtime."""

from __future__ import annotations

import statistics

from app.agents import indicators
from app.agents.base import BaseAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.schemas.market import Candle


class CandleSpecialistAgent(BaseAgent):
    """Read-only OHLCV specialist; never accesses execution or risk services."""

    required_inputs = ("candles.ohlcv",)
    decision_role = "SHADOW"
    critical = False

    def __init__(self, store: CandleStore) -> None:
        super().__init__()
        self._store = store

    def _candles(
        self,
        agent_input: AgentInput,
        *,
        limit: int = 200,
    ) -> list[Candle]:
        exchange = agent_input.market_context.get("exchange", "BINANCE")
        return self._store.get(
            exchange,
            agent_input.symbol,
            agent_input.timeframe,
            limit=limit,
        )

    def _insufficient(
        self,
        *,
        actual: int,
        required: int,
    ) -> AgentOutput:
        return self._output(
            AgentStatus.COMPLETED,
            Signal.WAIT,
            0,
            f"Insufficient candles ({actual}/{required})",
            evidence={"candle_count": actual, "required_candles": required},
            warnings=["INSUFFICIENT_HISTORY"],
        )


class MomentumAgent(CandleSpecialistAgent):
    name = "MomentumAgent"
    description = "Measures rate of change and RSI momentum"
    capabilities = ("rate-of-change", "rsi")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        if len(candles) < 15:
            return self._insufficient(actual=len(candles), required=15)
        closes = [candle.close for candle in candles]
        momentum = (closes[-1] / closes[-11] - 1) * 100
        rsi_value = indicators.rsi(closes) or 50.0
        if momentum > 0.5 and rsi_value < 75:
            signal = Signal.BUY
        elif momentum < -0.5 and rsi_value > 25:
            signal = Signal.SELL
        else:
            signal = Signal.HOLD
        confidence = min(90, int(45 + abs(momentum) * 10))
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"10-candle momentum {momentum:.3f}% with RSI {rsi_value:.2f}",
            evidence={
                "momentum_percent": round(momentum, 6),
                "rsi": round(rsi_value, 4),
            },
        )


class VolatilityAgent(CandleSpecialistAgent):
    name = "VolatilityAgent"
    description = "Classifies realized ATR volatility"
    capabilities = ("atr", "realized-volatility")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        if len(candles) < 15:
            return self._insufficient(actual=len(candles), required=15)
        atr_value = indicators.atr(candles)
        assert atr_value is not None
        atr_percent = atr_value / candles[-1].close * 100
        if atr_percent > 3:
            signal, confidence, state = Signal.WAIT, 85, "HIGH"
        elif atr_percent < 0.15:
            signal, confidence, state = Signal.HOLD, 70, "LOW"
        else:
            signal, confidence, state = Signal.NEUTRAL, 75, "NORMAL"
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"ATR is {atr_percent:.3f}% of price ({state})",
            evidence={
                "atr": round(atr_value, 6),
                "atr_percent": round(atr_percent, 6),
                "volatility_state": state,
            },
        )


class VolumeAgent(CandleSpecialistAgent):
    name = "VolumeAgent"
    description = "Detects participation using relative candle volume"
    capabilities = ("volume-ratio", "participation")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        if len(candles) < 21:
            return self._insufficient(actual=len(candles), required=21)
        ratio = indicators.volume_ratio(candles)
        assert ratio is not None
        direction = candles[-1].close - candles[-1].open
        if ratio >= 1.5 and direction > 0:
            signal = Signal.BUY
        elif ratio >= 1.5 and direction < 0:
            signal = Signal.SELL
        else:
            signal = Signal.NEUTRAL
        confidence = min(90, int(45 + ratio * 20))
        warnings = ["LOW_VOLUME"] if ratio < 0.5 else []
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"Latest volume is {ratio:.3f}x its 20-candle baseline",
            evidence={"volume_ratio": round(ratio, 6)},
            warnings=warnings,
        )


class VWAPAgent(CandleSpecialistAgent):
    name = "VWAPAgent"
    description = "Measures price displacement from rolling VWAP"
    capabilities = ("vwap", "price-displacement")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=50)
        if len(candles) < 20:
            return self._insufficient(actual=len(candles), required=20)
        vwap_value = indicators.vwap(candles)
        if vwap_value is None:
            return self._output(
                AgentStatus.COMPLETED,
                Signal.WAIT,
                0,
                "VWAP unavailable because aggregate volume is zero",
                warnings=["LOW_VOLUME"],
            )
        displacement = (candles[-1].close / vwap_value - 1) * 100
        signal = (
            Signal.BUY
            if displacement > 0.2
            else Signal.SELL
            if displacement < -0.2
            else Signal.HOLD
        )
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(90, int(50 + abs(displacement) * 20)),
            f"Price is {displacement:.3f}% from rolling VWAP",
            evidence={
                "vwap": round(vwap_value, 6),
                "displacement_percent": round(displacement, 6),
            },
        )


class MACDAgent(CandleSpecialistAgent):
    name = "MACDAgent"
    description = "Evaluates MACD direction and histogram"
    capabilities = ("macd",)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        closes = [candle.close for candle in candles]
        result = indicators.macd(closes)
        if result is None:
            return self._insufficient(actual=len(candles), required=35)
        line, signal_line, histogram = result
        signal = (
            Signal.BUY
            if histogram > 0
            else Signal.SELL
            if histogram < 0
            else Signal.HOLD
        )
        relative = abs(histogram) / candles[-1].close * 10_000
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(90, int(50 + relative * 3)),
            f"MACD histogram {histogram:.6f}",
            evidence={
                "macd_line": round(line, 8),
                "signal_line": round(signal_line, 8),
                "histogram": round(histogram, 8),
            },
        )


class EMAAlignmentAgent(CandleSpecialistAgent):
    name = "EMAAlignmentAgent"
    description = "Specializes in short/medium/long EMA alignment"
    capabilities = ("ema-alignment",)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        if len(candles) < 50:
            return self._insufficient(actual=len(candles), required=50)
        closes = [candle.close for candle in candles]
        ema9 = indicators.ema(closes, 9)[-1]
        ema21 = indicators.ema(closes, 21)[-1]
        ema50 = indicators.ema(closes, 50)[-1]
        if ema9 > ema21 > ema50:
            signal, alignment = Signal.BUY, "BULLISH"
        elif ema9 < ema21 < ema50:
            signal, alignment = Signal.SELL, "BEARISH"
        else:
            signal, alignment = Signal.HOLD, "MIXED"
        spread = abs(ema9 - ema50) / ema50 * 100
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(90, int(50 + spread * 15)),
            f"EMA alignment is {alignment}",
            evidence={
                "ema_9": round(ema9, 6),
                "ema_21": round(ema21, 6),
                "ema_50": round(ema50, 6),
                "alignment": alignment,
            },
        )


class MeanReversionAgent(CandleSpecialistAgent):
    name = "MeanReversionAgent"
    description = "Measures close-price z-score against a rolling baseline"
    capabilities = ("z-score", "mean-reversion")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=50)
        if len(candles) < 30:
            return self._insufficient(actual=len(candles), required=30)
        closes = [candle.close for candle in candles[-30:]]
        mean = statistics.fmean(closes)
        deviation = statistics.pstdev(closes)
        z_score = (closes[-1] - mean) / deviation if deviation > 0 else 0.0
        signal = (
            Signal.SELL
            if z_score >= 2
            else Signal.BUY
            if z_score <= -2
            else Signal.HOLD
        )
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(90, int(45 + abs(z_score) * 20)),
            f"30-candle close z-score {z_score:.3f}",
            evidence={
                "z_score": round(z_score, 6),
                "mean_close": round(mean, 6),
                "standard_deviation": round(deviation, 6),
            },
        )


class BreakoutAgent(CandleSpecialistAgent):
    name = "BreakoutAgent"
    description = "Detects closes outside the previous 20-candle range"
    capabilities = ("range-breakout",)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=30)
        if len(candles) < 21:
            return self._insufficient(actual=len(candles), required=21)
        prior = candles[-21:-1]
        resistance = max(candle.high for candle in prior)
        support = min(candle.low for candle in prior)
        close = candles[-1].close
        if close > resistance:
            signal, state = Signal.BUY, "UPSIDE_BREAKOUT"
        elif close < support:
            signal, state = Signal.SELL, "DOWNSIDE_BREAKOUT"
        else:
            signal, state = Signal.HOLD, "INSIDE_RANGE"
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            80 if signal in {Signal.BUY, Signal.SELL} else 55,
            f"Close classified as {state}",
            evidence={
                "support": round(support, 6),
                "resistance": round(resistance, 6),
                "state": state,
            },
        )


class SupportResistanceAgent(CandleSpecialistAgent):
    name = "SupportResistanceAgent"
    description = "Locates rolling support and resistance proximity"
    capabilities = ("support", "resistance")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=60)
        if len(candles) < 30:
            return self._insufficient(actual=len(candles), required=30)
        window = candles[-30:]
        support = min(candle.low for candle in window)
        resistance = max(candle.high for candle in window)
        price = window[-1].close
        range_size = max(resistance - support, price * 1e-9)
        location = (price - support) / range_size
        signal = (
            Signal.BUY
            if location <= 0.15
            else Signal.SELL
            if location >= 0.85
            else Signal.HOLD
        )
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            int(55 + abs(location - 0.5) * 50),
            f"Price location within 30-candle range is {location:.3f}",
            evidence={
                "support": round(support, 6),
                "resistance": round(resistance, 6),
                "range_location": round(location, 6),
            },
        )


class CandleStructureAgent(CandleSpecialistAgent):
    name = "CandleStructureAgent"
    description = "Classifies the latest candle body and wick structure"
    capabilities = ("candle-structure", "wick-analysis")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=2)
        if not candles:
            return self._insufficient(actual=0, required=1)
        candle = candles[-1]
        candle_range = max(candle.high - candle.low, candle.close * 1e-9)
        body = candle.close - candle.open
        body_ratio = abs(body) / candle_range
        upper_wick = candle.high - max(candle.open, candle.close)
        lower_wick = min(candle.open, candle.close) - candle.low
        if body_ratio >= 0.6 and body > 0:
            signal, structure = Signal.BUY, "BULLISH_IMPULSE"
        elif body_ratio >= 0.6 and body < 0:
            signal, structure = Signal.SELL, "BEARISH_IMPULSE"
        else:
            signal, structure = Signal.HOLD, "INDECISION"
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(85, int(45 + body_ratio * 50)),
            f"Latest candle structure is {structure}",
            evidence={
                "body_ratio": round(body_ratio, 6),
                "upper_wick_ratio": round(upper_wick / candle_range, 6),
                "lower_wick_ratio": round(lower_wick / candle_range, 6),
                "structure": structure,
            },
        )


class LiquidityProxyAgent(CandleSpecialistAgent):
    name = "LiquidityProxyAgent"
    description = "Estimates liquidity stress from public OHLCV proxies"
    capabilities = ("liquidity-proxy", "range-volume-stress")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=30)
        if len(candles) < 21:
            return self._insufficient(actual=len(candles), required=21)
        ratio = indicators.volume_ratio(candles)
        assert ratio is not None
        ranges = [
            (candle.high - candle.low) / candle.close
            for candle in candles[-21:-1]
        ]
        baseline_range = statistics.fmean(ranges)
        latest_range = (
            candles[-1].high - candles[-1].low
        ) / candles[-1].close
        stress = (
            latest_range / baseline_range
            if baseline_range > 0
            else 1.0
        )
        signal = Signal.WAIT if stress > 2 and ratio < 0.8 else Signal.NEUTRAL
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            min(90, int(50 + abs(stress - 1) * 20)),
            f"OHLCV liquidity stress proxy is {stress:.3f}",
            evidence={
                "range_stress_ratio": round(stress, 6),
                "volume_ratio": round(ratio, 6),
                "proxy_only": True,
            },
            warnings=["OHLCV_LIQUIDITY_PROXY"],
        )


class DataQualityAgent(CandleSpecialistAgent):
    name = "DataQualityAgent"
    description = "Independently checks the runtime candle window"
    capabilities = ("ordering-check", "duplicate-check", "ohlcv-invariants")

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input)
        if not candles:
            return self._output(
                AgentStatus.BLOCKED,
                Signal.BLOCK,
                0,
                "No candles available for independent quality validation",
                warnings=["DATA_QUALITY_ISSUE"],
            )
        timestamps = [candle.closed_at for candle in candles]
        ordered = timestamps == sorted(timestamps)
        unique = len(timestamps) == len(set(timestamps))
        valid = ordered and unique
        return self._output(
            AgentStatus.COMPLETED if valid else AgentStatus.BLOCKED,
            Signal.NEUTRAL if valid else Signal.BLOCK,
            100 if valid else 0,
            (
                "Runtime candle window is ordered and duplicate-free"
                if valid
                else "Runtime candle window failed quality invariants"
            ),
            evidence={
                "candle_count": len(candles),
                "strictly_ordered": ordered,
                "unique_timestamps": unique,
            },
            warnings=[] if valid else ["DATA_QUALITY_ISSUE"],
        )


def build_shadow_specialists(store: CandleStore) -> list[BaseAgent]:
    """Return the fixed Month 5 shadow cohort in deterministic order."""

    return [
        MomentumAgent(store),
        VolatilityAgent(store),
        VolumeAgent(store),
        VWAPAgent(store),
        MACDAgent(store),
        EMAAlignmentAgent(store),
        MeanReversionAgent(store),
        BreakoutAgent(store),
        SupportResistanceAgent(store),
        CandleStructureAgent(store),
        LiquidityProxyAgent(store),
        DataQualityAgent(store),
    ]

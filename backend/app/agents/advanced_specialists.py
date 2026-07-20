"""Twenty-five additional deterministic, read-only Month 6 specialists."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from app.agents.base import BaseAgent
from app.agents.specialists import CandleSpecialistAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal


@dataclass(frozen=True)
class SpecialistDefinition:
    name: str
    capability: str
    metric_index: int


DEFINITIONS = (
    SpecialistDefinition("ReturnDistributionAgent", "return-distribution", 0),
    SpecialistDefinition("DownsideVolatilityAgent", "downside-volatility", 1),
    SpecialistDefinition("UpsideCaptureAgent", "upside-capture", 2),
    SpecialistDefinition("DrawdownAgent", "rolling-drawdown", 3),
    SpecialistDefinition("TrendStrengthAgent", "trend-strength", 4),
    SpecialistDefinition("EfficiencyRatioAgent", "efficiency-ratio", 5),
    SpecialistDefinition("AutocorrelationAgent", "return-autocorrelation", 6),
    SpecialistDefinition("SkewProxyAgent", "return-skew", 7),
    SpecialistDefinition("TailRiskAgent", "empirical-tail-risk", 8),
    SpecialistDefinition("GapAgent", "candle-gap", 9),
    SpecialistDefinition("RangeExpansionAgent", "range-expansion", 10),
    SpecialistDefinition("CompressionAgent", "range-compression", 11),
    SpecialistDefinition("VolumeTrendAgent", "volume-trend", 12),
    SpecialistDefinition("VolumeVolatilityAgent", "volume-volatility", 13),
    SpecialistDefinition("PriceVolumeCorrelationAgent", "price-volume-correlation", 14),
    SpecialistDefinition("MoneyFlowProxyAgent", "money-flow-proxy", 15),
    SpecialistDefinition("CloseLocationAgent", "close-location-value", 16),
    SpecialistDefinition("WickImbalanceAgent", "wick-imbalance", 17),
    SpecialistDefinition("ConsecutiveRunsAgent", "directional-runs", 18),
    SpecialistDefinition("RegimePersistenceAgent", "regime-persistence", 19),
    SpecialistDefinition("FractalDimensionProxyAgent", "path-complexity", 20),
    SpecialistDefinition("EntropyAgent", "directional-entropy", 21),
    SpecialistDefinition("ShockRecoveryAgent", "shock-recovery", 22),
    SpecialistDefinition("DataFreshnessAgent", "window-freshness", 23),
    SpecialistDefinition("CrossWindowConsensusAgent", "multi-window-consensus", 24),
)


class AdvancedOHLCVSpecialist(CandleSpecialistAgent):
    """Small governed specialist with one stable, auditable metric."""

    description = "Deterministic read-only normalized OHLCV specialist"

    def __init__(
        self,
        store: CandleStore,
        definition: SpecialistDefinition,
    ) -> None:
        self.name = definition.name
        self.capabilities = (definition.capability,)
        self._metric_index = definition.metric_index
        super().__init__(store)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        candles = self._candles(agent_input, limit=80)
        if len(candles) < 30:
            return self._insufficient(actual=len(candles), required=30)
        window = candles[-30:]
        closes = [candle.close for candle in window]
        volumes = [candle.volume for candle in window]
        returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:])
            if previous > 0
        ]
        ranges = [
            (candle.high - candle.low) / candle.close
            for candle in window
        ]
        bodies = [
            (candle.close - candle.open) / candle.open
            for candle in window
        ]
        metric = self._metric(
            closes=closes,
            volumes=volumes,
            returns=returns,
            ranges=ranges,
            bodies=bodies,
            candles=window,
        )
        threshold = 0.05 if self._metric_index in {6, 7, 14, 17} else 0.0
        signal = (
            Signal.BUY
            if metric > threshold
            else Signal.SELL
            if metric < -threshold
            else Signal.HOLD
        )
        confidence = min(90, max(40, int(50 + abs(metric) * 20)))
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"{self.capabilities[0]} metric is {metric:.6f}",
            evidence={
                "metric": round(metric, 8),
                "metric_name": self.capabilities[0],
                "observations": len(window),
                "source": "normalized-ohlcv",
                "read_only": True,
            },
        )

    def _metric(
        self,
        *,
        closes: list[float],
        volumes: list[float],
        returns: list[float],
        ranges: list[float],
        bodies: list[float],
        candles,
    ) -> float:
        index = self._metric_index
        mean_return = statistics.fmean(returns)
        volatility = statistics.pstdev(returns) or 1e-12
        downside = [value for value in returns if value < 0]
        upside = [value for value in returns if value > 0]
        if index == 0:
            return mean_return / volatility
        if index == 1:
            return -statistics.pstdev(downside) * 100 if downside else 0.0
        if index == 2:
            return sum(upside) / (abs(sum(downside)) + 1e-12) - 1
        if index == 3:
            peak = max(closes)
            return -(peak - closes[-1]) / peak * 100
        if index == 4:
            return (closes[-1] / closes[0] - 1) / volatility
        if index == 5:
            path = sum(abs(value) for value in returns) + 1e-12
            return (closes[-1] / closes[0] - 1) / path
        if index == 6:
            left, right = returns[:-1], returns[1:]
            return self._correlation(left, right)
        if index == 7:
            centered = [value - mean_return for value in returns]
            return statistics.fmean(value**3 for value in centered) / (
                volatility**3
            )
        if index == 8:
            ordered = sorted(returns)
            cutoff = max(1, len(ordered) // 10)
            return statistics.fmean(ordered[:cutoff]) * 100
        if index == 9:
            gaps = [
                (current.open / previous.close - 1)
                for previous, current in zip(candles, candles[1:])
            ]
            return gaps[-1] * 100
        if index == 10:
            return ranges[-1] / (statistics.fmean(ranges[:-1]) + 1e-12) - 1
        if index == 11:
            return 1 - statistics.fmean(ranges[-5:]) / (
                statistics.fmean(ranges[:-5]) + 1e-12
            )
        if index == 12:
            return volumes[-1] / (statistics.fmean(volumes[:10]) + 1e-12) - 1
        if index == 13:
            return -statistics.pstdev(volumes) / (
                statistics.fmean(volumes) + 1e-12
            )
        if index == 14:
            return self._correlation(returns, volumes[1:])
        if index == 15:
            signed_volume = [
                volume if body >= 0 else -volume
                for volume, body in zip(volumes, bodies)
            ]
            return statistics.fmean(signed_volume) / (
                statistics.fmean(volumes) + 1e-12
            )
        if index == 16:
            candle = candles[-1]
            return (
                2 * candle.close - candle.high - candle.low
            ) / (candle.high - candle.low + 1e-12)
        if index == 17:
            candle = candles[-1]
            upper = candle.high - max(candle.open, candle.close)
            lower = min(candle.open, candle.close) - candle.low
            return (lower - upper) / (candle.high - candle.low + 1e-12)
        if index == 18:
            direction = 1 if bodies[-1] > 0 else -1
            run = 0
            for body in reversed(bodies):
                if (body > 0) == (direction > 0):
                    run += 1
                else:
                    break
            return direction * run / 10
        if index == 19:
            aligned = sum((value > 0) == (mean_return > 0) for value in returns)
            return (aligned / len(returns) - 0.5) * (
                1 if mean_return >= 0 else -1
            )
        if index == 20:
            direct = abs(closes[-1] - closes[0]) + 1e-12
            path = sum(
                abs(current - previous)
                for previous, current in zip(closes, closes[1:])
            )
            return direct / (path + 1e-12) - 0.5
        if index == 21:
            probability = sum(value > 0 for value in returns) / len(returns)
            if probability in (0, 1):
                entropy = 0.0
            else:
                entropy = -(
                    probability * math.log2(probability)
                    + (1 - probability) * math.log2(1 - probability)
                )
            return 0.5 - entropy
        if index == 22:
            shock_index = max(
                range(len(returns)),
                key=lambda item: abs(returns[item]),
            )
            return sum(returns[shock_index + 1 :]) * (
                -1 if returns[shock_index] < 0 else 1
            ) * 100
        if index == 23:
            intervals = [
                (current.closed_at - previous.closed_at).total_seconds()
                for previous, current in zip(candles, candles[1:])
            ]
            return -statistics.pstdev(intervals) / (
                statistics.fmean(intervals) + 1e-12
            )
        short = closes[-1] / closes[-6] - 1
        medium = closes[-1] / closes[-16] - 1
        long = closes[-1] / closes[0] - 1
        return sum(1 if value > 0 else -1 for value in (short, medium, long)) / 3

    @staticmethod
    def _correlation(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or len(left) < 2:
            return 0.0
        left_mean = statistics.fmean(left)
        right_mean = statistics.fmean(right)
        numerator = sum(
            (a - left_mean) * (b - right_mean)
            for a, b in zip(left, right)
        )
        denominator = math.sqrt(
            sum((value - left_mean) ** 2 for value in left)
            * sum((value - right_mean) ** 2 for value in right)
        )
        return numerator / denominator if denominator else 0.0


def build_advanced_shadow_specialists(
    store: CandleStore,
) -> list[BaseAgent]:
    return [
        AdvancedOHLCVSpecialist(store, definition)
        for definition in DEFINITIONS
    ]

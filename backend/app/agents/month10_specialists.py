"""Fifty deterministic Month 10 resilience-scale SHADOW specialists."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Literal

from app.agents.specialists import CandleSpecialistAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal

ResilienceFamily = Literal[
    "ENTROPY",
    "JUMP_INTENSITY",
    "VOLUME_ANOMALY",
    "GAP_PRESSURE",
    "TREND_PERSISTENCE",
    "VOL_OF_VOL",
    "CLOSE_LOCATION",
    "WICK_IMBALANCE",
    "DOWNSIDE_CAPTURE",
]


@dataclass(frozen=True)
class ResilienceDefinition:
    name: str
    family: ResilienceFamily
    window: int
    capability: str
    threshold: float
    positive_signal: Signal = Signal.BUY


def _definitions(
    prefix: str,
    family: ResilienceFamily,
    windows: tuple[int, ...],
    capability: str,
    *,
    threshold: float,
    positive_signal: Signal = Signal.BUY,
) -> tuple[ResilienceDefinition, ...]:
    return tuple(
        ResilienceDefinition(
            name=f"{prefix}{window}Agent",
            family=family,
            window=window,
            capability=f"{capability}-{window}",
            threshold=threshold,
            positive_signal=positive_signal,
        )
        for window in windows
    )


MONTH10_RESILIENCE_DEFINITIONS = (
    *_definitions(
        "ReturnEntropy",
        "ENTROPY",
        (8, 13, 21, 34, 55, 89),
        "return-entropy",
        threshold=0.08,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "JumpIntensity",
        "JUMP_INTENSITY",
        (8, 13, 21, 34, 55, 89),
        "jump-intensity",
        threshold=0.20,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "VolumeAnomaly",
        "VOLUME_ANOMALY",
        (8, 13, 21, 34, 55, 89),
        "volume-anomaly",
        threshold=0.20,
    ),
    *_definitions(
        "GapPressure",
        "GAP_PRESSURE",
        (8, 13, 21, 34, 55, 89),
        "gap-pressure",
        threshold=0.03,
    ),
    *_definitions(
        "TrendPersistence",
        "TREND_PERSISTENCE",
        (8, 13, 21, 34, 55, 89),
        "trend-persistence",
        threshold=0.10,
    ),
    *_definitions(
        "VolatilityOfVolatility",
        "VOL_OF_VOL",
        (10, 15, 20, 30, 45),
        "volatility-of-volatility",
        threshold=0.20,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "CloseLocation",
        "CLOSE_LOCATION",
        (8, 13, 21, 34, 55),
        "close-location-value",
        threshold=0.10,
    ),
    *_definitions(
        "WickImbalance",
        "WICK_IMBALANCE",
        (8, 13, 21, 34, 55),
        "wick-imbalance",
        threshold=0.10,
    ),
    *_definitions(
        "DownsideCapture",
        "DOWNSIDE_CAPTURE",
        (8, 13, 21, 34, 55),
        "downside-capture",
        threshold=0.20,
        positive_signal=Signal.SELL,
    ),
)


class Month10ResilienceSpecialist(CandleSpecialistAgent):
    """One-feature OHLCV SHADOW diagnostic with no execution dependency."""

    version = "1.0.0"
    description = "Deterministic read-only Month 10 scale diagnostic"

    def __init__(
        self,
        store: CandleStore,
        definition: ResilienceDefinition,
    ) -> None:
        self.name = definition.name
        self.capabilities = (definition.capability,)
        self._definition = definition
        super().__init__(store)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        required = self._required_observations()
        candles = self._candles(agent_input, limit=max(200, required))
        if len(candles) < required:
            return self._insufficient(actual=len(candles), required=required)
        metric = self._metric(candles)
        threshold = self._definition.threshold
        if metric > threshold:
            signal = self._definition.positive_signal
        elif metric < -threshold:
            signal = (
                Signal.SELL
                if self._definition.positive_signal == Signal.BUY
                else Signal.BUY
            )
        else:
            signal = Signal.HOLD
        confidence = min(
            90,
            max(40, int(45 + min(abs(metric), 1.5) * 30)),
        )
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            (
                f"{self._definition.capability} normalized diagnostic "
                f"is {metric:.8f}"
            ),
            evidence={
                "family": self._definition.family,
                "window": self._definition.window,
                "metric": round(metric, 10),
                "threshold": threshold,
                "source": "normalized-ohlcv",
                "read_only": True,
                "decision_authority": False,
                "order_authority": False,
            },
        )

    def _required_observations(self) -> int:
        if self._definition.family in {
            "VOLUME_ANOMALY",
            "VOL_OF_VOL",
        }:
            return 2 * self._definition.window + 1
        return self._definition.window + 1

    def _metric(self, candles) -> float:
        window = self._definition.window
        sample = candles[-window:]
        closes = [item.close for item in candles]
        returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:])
        ]
        recent_returns = returns[-window:]
        family = self._definition.family
        if family == "ENTROPY":
            positive = sum(item > 0 for item in recent_returns)
            negative = sum(item < 0 for item in recent_returns)
            probabilities = [
                count / window
                for count in (positive, negative, window - positive - negative)
                if count
            ]
            entropy = -sum(
                probability * math.log(probability, 2)
                for probability in probabilities
            )
            return entropy / math.log(3, 2) - 0.5
        if family == "JUMP_INTENSITY":
            scale = statistics.pstdev(recent_returns) + 1e-12
            jumps = sum(abs(item) > 2.5 * scale for item in recent_returns)
            return jumps / window
        if family == "VOLUME_ANOMALY":
            recent = statistics.fmean(
                item.volume for item in candles[-window:]
            )
            reference = statistics.fmean(
                item.volume for item in candles[-2 * window : -window]
            )
            return recent / (reference + 1e-12) - 1
        if family == "GAP_PRESSURE":
            gaps = [
                current.open / previous.close - 1
                for previous, current in zip(sample, sample[1:])
            ]
            return statistics.fmean(gaps) if gaps else 0.0
        if family == "TREND_PERSISTENCE":
            positive = sum(item > 0 for item in recent_returns)
            negative = sum(item < 0 for item in recent_returns)
            return (positive - negative) / window
        if family == "VOL_OF_VOL":
            recent = [
                abs(item)
                for item in returns[-window:]
            ]
            reference = [
                abs(item)
                for item in returns[-2 * window : -window]
            ]
            return (
                statistics.pstdev(recent)
                / (statistics.pstdev(reference) + 1e-12)
                - 1
            )
        if family == "CLOSE_LOCATION":
            values = [
                (
                    2 * item.close - item.high - item.low
                )
                / (item.high - item.low + 1e-12)
                for item in sample
            ]
            return statistics.fmean(values)
        if family == "WICK_IMBALANCE":
            values = []
            for item in sample:
                upper = item.high - max(item.open, item.close)
                lower = min(item.open, item.close) - item.low
                values.append(
                    (lower - upper) / (item.high - item.low + 1e-12)
                )
            return statistics.fmean(values)
        downside = sum(
            item * item for item in recent_returns if item < 0
        )
        upside = sum(
            item * item for item in recent_returns if item > 0
        )
        return (downside - upside) / (downside + upside + 1e-12)


def build_month10_shadow_specialists(
    store: CandleStore,
) -> list[Month10ResilienceSpecialist]:
    return [
        Month10ResilienceSpecialist(store, definition)
        for definition in MONTH10_RESILIENCE_DEFINITIONS
    ]

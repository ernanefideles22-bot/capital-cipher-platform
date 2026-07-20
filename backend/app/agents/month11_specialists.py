"""One hundred deterministic Month 11 PAPER shadow validators."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Literal

from app.agents.specialists import CandleSpecialistAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal

ValidationFamily = Literal[
    "DOWNSIDE_DEVIATION",
    "UPSIDE_DEVIATION",
    "RETURN_SKEW",
    "TAIL_RATIO",
    "VOLUME_TREND",
    "RANGE_EXPANSION",
    "RETURN_ACCELERATION",
    "DRAWDOWN_DEPTH",
    "RECOVERY_STRENGTH",
    "PRICE_VOLUME_CORRELATION",
]


@dataclass(frozen=True)
class ShadowValidationDefinition:
    name: str
    family: ValidationFamily
    window: int
    capability: str
    threshold: float
    positive_signal: Signal = Signal.BUY


def _definitions(
    prefix: str,
    family: ValidationFamily,
    capability: str,
    *,
    threshold: float,
    positive_signal: Signal = Signal.BUY,
) -> tuple[ShadowValidationDefinition, ...]:
    return tuple(
        ShadowValidationDefinition(
            name=f"{prefix}{window}Agent",
            family=family,
            window=window,
            capability=f"{capability}-{window}",
            threshold=threshold,
            positive_signal=positive_signal,
        )
        for window in (8, 13, 21, 34, 55, 72, 89, 120, 144, 180)
    )


MONTH11_SHADOW_VALIDATION_DEFINITIONS = (
    *_definitions(
        "DownsideDeviation",
        "DOWNSIDE_DEVIATION",
        "downside-deviation",
        threshold=0.004,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "UpsideDeviation",
        "UPSIDE_DEVIATION",
        "upside-deviation",
        threshold=0.004,
    ),
    *_definitions(
        "ReturnSkew",
        "RETURN_SKEW",
        "return-skew",
        threshold=0.20,
    ),
    *_definitions(
        "TailRatio",
        "TAIL_RATIO",
        "tail-ratio",
        threshold=0.20,
    ),
    *_definitions(
        "VolumeTrend",
        "VOLUME_TREND",
        "volume-trend",
        threshold=0.10,
    ),
    *_definitions(
        "RangeExpansion",
        "RANGE_EXPANSION",
        "range-expansion",
        threshold=0.15,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "ReturnAcceleration",
        "RETURN_ACCELERATION",
        "return-acceleration",
        threshold=0.001,
    ),
    *_definitions(
        "DrawdownDepth",
        "DRAWDOWN_DEPTH",
        "drawdown-depth",
        threshold=0.02,
        positive_signal=Signal.SELL,
    ),
    *_definitions(
        "RecoveryStrength",
        "RECOVERY_STRENGTH",
        "recovery-strength",
        threshold=0.02,
    ),
    *_definitions(
        "PriceVolumeCorrelation",
        "PRICE_VOLUME_CORRELATION",
        "price-volume-correlation",
        threshold=0.20,
    ),
)


class Month11ShadowValidationSpecialist(CandleSpecialistAgent):
    """Single-feature, read-only diagnostic without decision authority."""

    version = "1.0.0"
    description = "Deterministic read-only Month 11 shadow validator"

    def __init__(
        self,
        store: CandleStore,
        definition: ShadowValidationDefinition,
    ) -> None:
        self.name = definition.name
        self.capabilities = (definition.capability,)
        self._definition = definition
        super().__init__(store)

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        required = self._required_observations()
        candles = self._candles(agent_input, limit=max(400, required))
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
        confidence = min(90, max(40, int(45 + min(abs(metric), 1.5) * 30)))
        return self._output(
            AgentStatus.COMPLETED,
            signal,
            confidence,
            f"{self._definition.capability} diagnostic is {metric:.8f}",
            evidence={
                "family": self._definition.family,
                "window": self._definition.window,
                "metric": round(metric, 10),
                "threshold": threshold,
                "source": "normalized-ohlcv",
                "read_only": True,
                "decision_authority": False,
                "risk_authority": False,
                "order_authority": False,
            },
        )

    def _required_observations(self) -> int:
        if self._definition.family in {"VOLUME_TREND", "RANGE_EXPANSION"}:
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
        recent = returns[-window:]
        family = self._definition.family
        if family == "DOWNSIDE_DEVIATION":
            losses = [min(value, 0.0) for value in recent]
            return math.sqrt(statistics.fmean(value * value for value in losses))
        if family == "UPSIDE_DEVIATION":
            gains = [max(value, 0.0) for value in recent]
            return math.sqrt(statistics.fmean(value * value for value in gains))
        if family == "RETURN_SKEW":
            mean = statistics.fmean(recent)
            deviation = statistics.pstdev(recent) + 1e-12
            return statistics.fmean(
                ((value - mean) / deviation) ** 3 for value in recent
            )
        if family == "TAIL_RATIO":
            ordered = sorted(recent)
            tail = max(1, window // 10)
            upper = statistics.fmean(ordered[-tail:])
            lower = abs(statistics.fmean(ordered[:tail])) + 1e-12
            return upper / lower - 1
        if family == "VOLUME_TREND":
            current = statistics.fmean(item.volume for item in candles[-window:])
            previous = statistics.fmean(
                item.volume for item in candles[-2 * window : -window]
            )
            return current / (previous + 1e-12) - 1
        if family == "RANGE_EXPANSION":
            current = statistics.fmean(
                (item.high - item.low) / item.close for item in candles[-window:]
            )
            previous = statistics.fmean(
                (item.high - item.low) / item.close
                for item in candles[-2 * window : -window]
            )
            return current / (previous + 1e-12) - 1
        if family == "RETURN_ACCELERATION":
            split = max(1, len(recent) // 2)
            return statistics.fmean(recent[split:]) - statistics.fmean(recent[:split])
        if family == "DRAWDOWN_DEPTH":
            peak = sample[0].close
            deepest = 0.0
            for item in sample:
                peak = max(peak, item.close)
                deepest = max(deepest, (peak - item.close) / peak)
            return deepest
        if family == "RECOVERY_STRENGTH":
            trough = min(item.close for item in sample)
            return sample[-1].close / trough - 1
        price_changes = recent
        volumes = [item.volume for item in sample]
        if statistics.pstdev(price_changes) <= 1e-12 or statistics.pstdev(volumes) <= 1e-12:
            return 0.0
        return statistics.correlation(price_changes, volumes)


def build_month11_shadow_specialists(
    store: CandleStore,
) -> list[Month11ShadowValidationSpecialist]:
    return [
        Month11ShadowValidationSpecialist(store, definition)
        for definition in MONTH11_SHADOW_VALIDATION_DEFINITIONS
    ]

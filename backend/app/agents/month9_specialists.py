"""Fifty deterministic Month 9 diagnostic shadow specialists."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Literal

from app.agents.specialists import CandleSpecialistAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal

DiagnosticFamily = Literal[
    "RETURN",
    "VOLATILITY",
    "DRAWDOWN",
    "VOLUME_PRESSURE",
    "RANGE_EFFICIENCY",
    "TAIL_BALANCE",
    "AUTOCORRELATION",
    "LIQUIDITY",
]


@dataclass(frozen=True)
class DiagnosticDefinition:
    name: str
    family: DiagnosticFamily
    window: int
    capability: str
    positive_signal: Signal = Signal.BUY
    threshold: float = 0.02


def _definitions(
    prefix: str,
    family: DiagnosticFamily,
    windows: tuple[int, ...],
    capability: str,
    *,
    positive_signal: Signal = Signal.BUY,
    threshold: float = 0.02,
) -> tuple[DiagnosticDefinition, ...]:
    return tuple(
        DiagnosticDefinition(
            name=f"{prefix}{window}Agent",
            family=family,
            window=window,
            capability=f"{capability}-{window}",
            positive_signal=positive_signal,
            threshold=threshold,
        )
        for window in windows
    )


RETURN_DEFINITIONS = _definitions(
    "ReturnHorizon",
    "RETURN",
    (2, 3, 5, 8, 13, 21, 34, 55),
    "return-horizon",
    threshold=0.001,
)
VOLATILITY_DEFINITIONS = _definitions(
    "VolatilityShift",
    "VOLATILITY",
    (5, 8, 13, 21, 34, 55, 72, 96),
    "volatility-shift",
    positive_signal=Signal.SELL,
    threshold=0.10,
)
DRAWDOWN_DEFINITIONS = _definitions(
    "DrawdownWindow",
    "DRAWDOWN",
    (10, 15, 20, 30, 45, 60),
    "drawdown-window",
    threshold=0.01,
)
VOLUME_PRESSURE_DEFINITIONS = _definitions(
    "VolumePressure",
    "VOLUME_PRESSURE",
    (5, 10, 15, 20, 30, 40),
    "signed-volume-pressure",
    threshold=0.05,
)
RANGE_EFFICIENCY_DEFINITIONS = _definitions(
    "RangeEfficiency",
    "RANGE_EFFICIENCY",
    (5, 10, 15, 20, 30, 40),
    "range-efficiency",
    threshold=0.05,
)
TAIL_BALANCE_DEFINITIONS = _definitions(
    "TailBalance",
    "TAIL_BALANCE",
    (15, 20, 30, 45, 60),
    "tail-balance",
    threshold=0.05,
)
AUTOCORRELATION_DEFINITIONS = _definitions(
    "ReturnAutocorrelation",
    "AUTOCORRELATION",
    (1, 2, 3, 4, 5),
    "return-autocorrelation-lag",
    threshold=0.10,
)
LIQUIDITY_DEFINITIONS = _definitions(
    "LiquidityShift",
    "LIQUIDITY",
    (10, 20, 30, 40, 60, 80),
    "ohlcv-liquidity-shift",
    threshold=0.10,
)

MONTH9_DIAGNOSTIC_DEFINITIONS = (
    *RETURN_DEFINITIONS,
    *VOLATILITY_DEFINITIONS,
    *DRAWDOWN_DEFINITIONS,
    *VOLUME_PRESSURE_DEFINITIONS,
    *RANGE_EFFICIENCY_DEFINITIONS,
    *TAIL_BALANCE_DEFINITIONS,
    *AUTOCORRELATION_DEFINITIONS,
    *LIQUIDITY_DEFINITIONS,
)


class Month9DiagnosticSpecialist(CandleSpecialistAgent):
    """One-feature OHLCV diagnostic with no risk or execution dependency."""

    version = "1.0.0"
    description = "Deterministic read-only Month 9 portfolio diagnostic"

    def __init__(
        self,
        store: CandleStore,
        definition: DiagnosticDefinition,
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
            },
        )

    def _required_observations(self) -> int:
        window = self._definition.window
        if self._definition.family in {"VOLATILITY", "LIQUIDITY"}:
            return 2 * window + 1
        if self._definition.family == "AUTOCORRELATION":
            return max(30, window + 12)
        return window + 1

    def _metric(self, candles) -> float:
        window = self._definition.window
        closes = [item.close for item in candles]
        volumes = [item.volume for item in candles]
        returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:])
        ]
        family = self._definition.family
        if family == "RETURN":
            return closes[-1] / closes[-window - 1] - 1
        if family == "VOLATILITY":
            recent = statistics.pstdev(returns[-window:]) or 0.0
            reference = statistics.pstdev(
                returns[-2 * window : -window]
            )
            return recent / (reference + 1e-12) - 1
        if family == "DRAWDOWN":
            peak = max(closes[-window:])
            return closes[-1] / peak - 1
        if family == "VOLUME_PRESSURE":
            paired = zip(volumes[-window:], returns[-window:])
            signed = sum(
                volume if change > 0 else -volume if change < 0 else 0
                for volume, change in paired
            )
            return signed / (sum(volumes[-window:]) + 1e-12)
        if family == "RANGE_EFFICIENCY":
            path = sum(abs(item) for item in returns[-window:])
            net = closes[-1] / closes[-window - 1] - 1
            return net / (path + 1e-12)
        if family == "TAIL_BALANCE":
            sample = returns[-window:]
            upside = sum(item * item for item in sample if item > 0)
            downside = sum(item * item for item in sample if item < 0)
            return (upside - downside) / (upside + downside + 1e-12)
        if family == "AUTOCORRELATION":
            lag = window
            sample = returns[-30:]
            left, right = sample[:-lag], sample[lag:]
            left_mean = statistics.fmean(left)
            right_mean = statistics.fmean(right)
            covariance = statistics.fmean(
                (a - left_mean) * (b - right_mean)
                for a, b in zip(left, right)
            )
            denominator = (
                statistics.pstdev(left) * statistics.pstdev(right)
            )
            return covariance / (denominator + 1e-12)
        recent = candles[-window:]
        reference = candles[-2 * window : -window]

        def liquidity(sample) -> float:
            dollar_volume = statistics.fmean(
                item.close * item.volume for item in sample
            )
            range_percent = statistics.fmean(
                (item.high - item.low) / item.close for item in sample
            )
            return dollar_volume / (range_percent + 1e-12)

        return liquidity(recent) / (liquidity(reference) + 1e-12) - 1


def build_month9_shadow_specialists(
    store: CandleStore,
) -> list[Month9DiagnosticSpecialist]:
    return [
        Month9DiagnosticSpecialist(store, definition)
        for definition in MONTH9_DIAGNOSTIC_DEFINITIONS
    ]

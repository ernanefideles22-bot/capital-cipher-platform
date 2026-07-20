"""Bounded in-process metrics with deterministic snapshot aggregation."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from app.schemas.operations import (
    OperationalMetricPoint,
    OperationalMetricSnapshot,
)


@dataclass(frozen=True)
class MetricSummary:
    count: int
    total: float
    average: float
    p95: float
    maximum: float


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil(percentile_value * len(ordered)) - 1,
        ),
    )
    return ordered[rank]


class BoundedMetricRegistry:
    """Keeps counters/gauges plus bounded histogram windows."""

    def __init__(
        self,
        *,
        max_samples_per_metric: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 100 <= max_samples_per_metric <= 1_000_000:
            raise ValueError("Metric sample capacity must be 100..1000000")
        self._capacity = max_samples_per_metric
        self._clock = clock
        self._counters: dict[str, float] = defaultdict(float)
        self._counter_events: dict[
            str,
            deque[tuple[float, float]],
        ] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[
            str,
            deque[tuple[float, float]],
        ] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def increment(self, name: str, amount: float = 1.0) -> None:
        if amount < 0 or not math.isfinite(amount):
            raise ValueError("Counter increments must be finite and nonnegative")
        self._counters[name] += amount
        events = self._counter_events.setdefault(
            name,
            deque(maxlen=self._capacity),
        )
        events.append((self._clock(), amount))

    def gauge(self, name: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("Gauge values must be finite")
        self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("Histogram observations must be finite")
        samples = self._histograms.setdefault(
            name,
            deque(maxlen=self._capacity),
        )
        samples.append((self._clock(), value))

    def counter(
        self,
        name: str,
        *,
        window_seconds: int | None = None,
    ) -> float:
        if window_seconds is not None:
            cutoff = self._clock() - window_seconds
            return sum(
                value
                for observed_at, value in self._counter_events.get(name, ())
                if observed_at >= cutoff
            )
        return self._counters.get(name, 0.0)

    def gauge_value(self, name: str) -> float | None:
        return self._gauges.get(name)

    def summary(
        self,
        name: str,
        *,
        window_seconds: int | None = None,
    ) -> MetricSummary:
        observations = self._histograms.get(name, ())
        cutoff = (
            self._clock() - window_seconds
            if window_seconds is not None
            else None
        )
        values = [
            value
            for observed_at, value in observations
            if cutoff is None or observed_at >= cutoff
        ]
        if not values:
            return MetricSummary(0, 0.0, 0.0, 0.0, 0.0)
        total = sum(values)
        return MetricSummary(
            count=len(values),
            total=total,
            average=total / len(values),
            p95=percentile(values, 0.95),
            maximum=max(values),
        )

    def sample_count(
        self,
        name: str,
        *,
        window_seconds: int | None = None,
    ) -> int:
        return self.summary(
            name,
            window_seconds=window_seconds,
        ).count

    def snapshot(
        self,
        *,
        correlation_id: str,
        window_seconds: int,
        registered_agents: int,
        active_agents: int,
    ) -> OperationalMetricSnapshot:
        points: list[OperationalMetricPoint] = []
        for name in sorted(self._counters):
            value = self.counter(
                name,
                window_seconds=window_seconds,
            )
            points.append(
                OperationalMetricPoint(
                    name=name,
                    kind="COUNTER",
                    value=round(value, 8),
                    sample_count=int(value),
                )
            )
        for name, value in sorted(self._gauges.items()):
            points.append(
                OperationalMetricPoint(
                    name=name,
                    kind="GAUGE",
                    value=round(value, 8),
                )
            )
        for name in sorted(self._histograms):
            summary = self.summary(
                name,
                window_seconds=window_seconds,
            )
            for suffix, value in (
                ("avg", summary.average),
                ("p95", summary.p95),
                ("max", summary.maximum),
            ):
                points.append(
                    OperationalMetricPoint(
                        name=f"{name}.{suffix}",
                        kind="HISTOGRAM",
                        value=round(value, 8),
                        sample_count=summary.count,
                    )
                )
        if not points:
            points.append(
                OperationalMetricPoint(
                    name="operations.started",
                    kind="GAUGE",
                    value=1,
                )
            )
        return OperationalMetricSnapshot(
            correlation_id=correlation_id,
            window_seconds=window_seconds,
            registered_agents=registered_agents,
            active_agents=active_agents,
            metrics=points,
        )

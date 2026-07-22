"""Content-addressed historical spread and funding observations."""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime

from app.backtesting.artifacts import canonical_sha256
from app.schemas.backtest import (
    HistoricalExecutionDataset,
    HistoricalExecutionDatasetManifest,
    HistoricalExecutionObservation,
)
from app.schemas.market import Candle


def build_historical_execution_manifest(
    dataset: HistoricalExecutionDataset,
) -> HistoricalExecutionDatasetManifest:
    dataset_hash = canonical_sha256(dataset.model_dump(mode="json"))
    observations = dataset.observations
    return HistoricalExecutionDatasetManifest(
        dataset_id=f"historical-execution:v1:{dataset_hash}",
        dataset_hash=dataset_hash,
        source=dataset.source,
        exchange=dataset.exchange,
        symbol=dataset.symbol,
        row_count=len(observations),
        start_at=observations[0].observed_at,
        end_at=observations[-1].observed_at,
        max_age_seconds=dataset.max_age_seconds,
    )


class HistoricalExecutionResolver:
    """Resolve only observations that existed at or before simulation time."""

    def __init__(self, dataset: HistoricalExecutionDataset) -> None:
        self.dataset = dataset
        self.manifest = build_historical_execution_manifest(dataset)
        self._observations = dataset.observations
        self._timestamps = [
            observation.observed_at for observation in self._observations
        ]

    def validate_candles(self, candles: list[Candle]) -> None:
        if not candles:
            raise ValueError(
                "Historical execution validation requires candles"
            )
        for candle in candles:
            if (
                candle.exchange != self.dataset.exchange
                or candle.symbol != self.dataset.symbol
            ):
                raise ValueError(
                    "Historical execution dataset does not match candle series"
                )
            self.resolve(candle.closed_at)

    def resolve(self, at: datetime) -> HistoricalExecutionObservation:
        index = bisect_right(self._timestamps, at) - 1
        if index < 0:
            raise ValueError(
                "Historical execution dataset has no observation available "
                f"at {at.isoformat()}"
            )
        observation = self._observations[index]
        age_seconds = (at - observation.observed_at).total_seconds()
        if age_seconds > self.dataset.max_age_seconds:
            raise ValueError(
                "Historical execution observation is stale at "
                f"{at.isoformat()}: age={age_seconds:.0f}s exceeds "
                f"{self.dataset.max_age_seconds}s"
            )
        return observation

    def funding_cost(
        self,
        *,
        position_notional: float,
        direction: float,
        start_at: datetime,
        end_at: datetime,
    ) -> float:
        if end_at <= start_at:
            return 0.0

        current_at = start_at
        current = self.resolve(start_at)
        total = 0.0
        next_index = bisect_right(self._timestamps, start_at)
        while current_at < end_at:
            next_change = (
                self._timestamps[next_index]
                if next_index < len(self._timestamps)
                else end_at
            )
            segment_end = min(next_change, end_at)
            segment_seconds = (segment_end - current_at).total_seconds()
            maximum_seconds = self.dataset.max_age_seconds - (
                current_at - current.observed_at
            ).total_seconds()
            if segment_seconds > maximum_seconds:
                stale_at = current.observed_at.isoformat()
                raise ValueError(
                    "Historical funding observation becomes stale before "
                    f"{segment_end.isoformat()}; source observation={stale_at}"
                )
            elapsed_hours = segment_seconds / 3_600
            rate = current.funding_rate_bps_per_8h / 10_000
            total += (
                position_notional
                * rate
                * (elapsed_hours / 8.0)
                * direction
            )
            current_at = segment_end
            if current_at == next_change and current_at < end_at:
                current = self._observations[next_index]
                next_index += 1
        return total

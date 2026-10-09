"""Live market-data continuity before agent evaluation.

A transient WebSocket interruption must not make the 300-agent cohort reason
about a candle series with holes. This module reuses the existing historical
backfill service to repair missing *closed* candles, hydrates the in-memory
CandleStore, and then allows the current live candle to reach the Orchestrator.
Recovered or persisted historical candles never generate retroactive trading
decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from app.core.logging import ServiceLogger
from app.market_data.data_quality import TIMEFRAME_SECONDS
from app.market_data.store import MAX_CANDLES, CandleStore
from app.schemas.backfill import HistoricalBackfillRequest
from app.schemas.market import Candle

logger = ServiceLogger("market-continuity")

# The largest current candle-window specialists require up to 180 observations
# (including derived multi-window diagnostics). Seed at least this many when
# public history is available so a deploy does not force hours of WAIT outputs.
DEFAULT_SPECIALIST_HISTORY_CANDLES = 180


class CandleRepository(Protocol):
    async def list_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100_000,
    ) -> list[Candle]: ...


class BackfillRunner(Protocol):
    async def run(self, request: HistoricalBackfillRequest) -> Any: ...


class CandleOrchestrator(Protocol):
    async def on_candle_closed(self, candle: Candle) -> Any: ...


class LiveCandleContinuityProcessor:
    """Hydrate/repair market history before each current-candle decision.

    The processor is intentionally synchronous with respect to the downstream
    candle-dispatch consumer: the current candle waits for history hydration or
    repair, while the exchange WebSocket receive loop remains free to keep
    ingesting messages into its bounded queue.
    """

    def __init__(
        self,
        *,
        store: CandleStore,
        repository: CandleRepository,
        backfill_service: BackfillRunner,
        orchestrator: CandleOrchestrator,
        history_limit: int = MAX_CANDLES,
        specialist_history_candles: int = DEFAULT_SPECIALIST_HISTORY_CANDLES,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        if specialist_history_candles < 1:
            raise ValueError("specialist_history_candles must be positive")
        if specialist_history_candles > history_limit:
            raise ValueError(
                "specialist_history_candles cannot exceed history_limit"
            )
        self._store = store
        self._repository = repository
        self._backfill_service = backfill_service
        self._orchestrator = orchestrator
        self._history_limit = history_limit
        self._specialist_history_candles = specialist_history_candles

    @staticmethod
    def _contiguous_suffix(
        candles: list[Candle],
        *,
        step: timedelta,
    ) -> list[Candle]:
        if not candles:
            return []
        ordered = sorted(candles, key=lambda item: item.closed_at)
        suffix_reversed = [ordered[-1]]
        expected_previous = ordered[-1].closed_at - step
        for item in reversed(ordered[:-1]):
            if item.closed_at == expected_previous:
                suffix_reversed.append(item)
                expected_previous -= step
                continue
            if item.closed_at < expected_previous:
                break
            # Duplicate/unexpected newer timestamps are ignored. The persisted
            # candle identity normally prevents this condition.
        return list(reversed(suffix_reversed))

    async def _load_persisted_history(
        self,
        candle: Candle,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[Candle]:
        return await self._repository.list_candles(
            exchange=candle.exchange.value,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            start_at=start_at,
            end_at=end_at,
            limit=self._history_limit,
        )

    async def _hydrate_persisted_history(
        self,
        candle: Candle,
        *,
        step_seconds: int,
    ) -> bool:
        """Seed an empty in-memory series from a verified contiguous suffix.

        Persistence is queried only up to the candle immediately preceding the
        current live close, which prevents look-ahead. If the newest contiguous
        suffix is too short for the current specialist cohort, the existing
        governed public backfill service repairs only the required recent
        history window. Historical rows hydrate state only and never reach the
        Orchestrator.
        """

        step = timedelta(seconds=step_seconds)
        history_end = candle.closed_at - step
        history_start = candle.closed_at - (step * self._history_limit)
        try:
            persisted = await self._load_persisted_history(
                candle,
                start_at=history_start,
                end_at=history_end,
            )
        except Exception as exc:
            logger.error(
                "Persisted candle history hydration failed",
                event_type="MARKET_HISTORY_HYDRATION_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "error_type": type(exc).__name__,
                },
            )
            return False

        eligible = [item for item in persisted if item.closed_at <= history_end]
        suffix = self._contiguous_suffix(eligible, step=step)

        if len(suffix) < self._specialist_history_candles:
            repair_start = candle.closed_at - (
                step * self._specialist_history_candles
            )
            request = HistoricalBackfillRequest(
                exchange=candle.exchange,
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                start_at=repair_start,
                end_at=history_end,
                max_candles=self._specialist_history_candles,
            )
            try:
                repair = await self._backfill_service.run(request)
                repair_status = getattr(repair, "status", None)
                if repair_status == "COMPLETED":
                    persisted = await self._load_persisted_history(
                        candle,
                        start_at=history_start,
                        end_at=history_end,
                    )
                    eligible = [
                        item
                        for item in persisted
                        if item.closed_at <= history_end
                    ]
                    suffix = self._contiguous_suffix(eligible, step=step)
                else:
                    logger.warning(
                        "Specialist history repair did not complete; using safe suffix",
                        event_type="MARKET_HISTORY_REPAIR_INCOMPLETE",
                        metadata={
                            "exchange": candle.exchange.value,
                            "symbol": candle.symbol,
                            "timeframe": candle.timeframe,
                            "status": repair_status or "UNKNOWN",
                            "available_contiguous_candles": len(suffix),
                            "required_contiguous_candles": (
                                self._specialist_history_candles
                            ),
                        },
                    )
            except Exception as exc:
                # Deep specialist history is an availability enhancement. The
                # current cycle can still proceed with the verified suffix; each
                # specialist independently fails closed to WAIT if its own window
                # is insufficient. Recent live gaps remain strictly fail-closed
                # in handle().
                logger.warning(
                    "Specialist history repair failed; using safe suffix",
                    event_type="MARKET_HISTORY_REPAIR_INCOMPLETE",
                    metadata={
                        "exchange": candle.exchange.value,
                        "symbol": candle.symbol,
                        "timeframe": candle.timeframe,
                        "error_type": type(exc).__name__,
                        "available_contiguous_candles": len(suffix),
                        "required_contiguous_candles": (
                            self._specialist_history_candles
                        ),
                    },
                )

        if not suffix:
            logger.info(
                "No persisted candle history available for initial hydration",
                event_type="MARKET_HISTORY_HYDRATED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "hydrated_count": 0,
                },
            )
            return True

        try:
            for historical_candle in suffix:
                self._store.add(historical_candle)
        except Exception as exc:
            logger.error(
                "Persisted candle history could not hydrate in-memory state",
                event_type="MARKET_HISTORY_HYDRATION_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "error_type": type(exc).__name__,
                },
            )
            return False

        logger.info(
            "Persisted candle history hydrated before agent evaluation",
            event_type="MARKET_HISTORY_HYDRATED",
            metadata={
                "exchange": candle.exchange.value,
                "symbol": candle.symbol,
                "timeframe": candle.timeframe,
                "hydrated_count": len(suffix),
                "persisted_rows_considered": len(eligible),
                "specialist_history_target": self._specialist_history_candles,
                "first_closed_at": suffix[0].closed_at.isoformat(),
                "last_closed_at": suffix[-1].closed_at.isoformat(),
            },
        )
        return True

    async def handle(self, candle: Candle) -> Any:
        previous = self._store.latest(
            candle.exchange.value,
            candle.symbol,
            candle.timeframe,
        )

        step_seconds = TIMEFRAME_SECONDS.get(candle.timeframe)
        if previous is None and step_seconds is not None:
            hydrated = await self._hydrate_persisted_history(
                candle,
                step_seconds=step_seconds,
            )
            if not hydrated:
                return None
            previous = self._store.latest(
                candle.exchange.value,
                candle.symbol,
                candle.timeframe,
            )

        if previous is None:
            return await self._orchestrator.on_candle_closed(candle)

        if step_seconds is None:
            logger.error(
                "Live candle continuity blocked unsupported timeframe",
                event_type="MARKET_GAP_RECOVERY_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "reason": "UNSUPPORTED_TIMEFRAME",
                },
            )
            return None

        delta_seconds = (candle.closed_at - previous.closed_at).total_seconds()
        if delta_seconds <= step_seconds * 1.5:
            return await self._orchestrator.on_candle_closed(candle)

        interval_count = delta_seconds / step_seconds
        rounded_intervals = round(interval_count)
        if rounded_intervals < 2 or abs(interval_count - rounded_intervals) > 1e-6:
            logger.error(
                "Live candle continuity blocked irregular interval",
                event_type="MARKET_GAP_RECOVERY_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "previous_closed_at": previous.closed_at.isoformat(),
                    "current_closed_at": candle.closed_at.isoformat(),
                    "delta_seconds": delta_seconds,
                    "reason": "IRREGULAR_INTERVAL",
                },
            )
            return None

        step = timedelta(seconds=step_seconds)
        gap_start = previous.closed_at + step
        gap_end = candle.closed_at - step
        missing_count = rounded_intervals - 1

        logger.warning(
            "Closed-candle gap detected; repairing before agent evaluation",
            event_type="MARKET_GAP_RECOVERY_STARTED",
            metadata={
                "exchange": candle.exchange.value,
                "symbol": candle.symbol,
                "timeframe": candle.timeframe,
                "start_at": gap_start.isoformat(),
                "end_at": gap_end.isoformat(),
                "missing_count": missing_count,
            },
        )

        request = HistoricalBackfillRequest(
            exchange=candle.exchange,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            start_at=gap_start,
            end_at=gap_end,
            max_candles=missing_count,
        )
        try:
            result = await self._backfill_service.run(request)
        except Exception as exc:
            logger.error(
                "Live candle gap recovery failed",
                event_type="MARKET_GAP_RECOVERY_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "reason": "BACKFILL_EXCEPTION",
                    "error_type": type(exc).__name__,
                },
            )
            return None

        if getattr(result, "status", None) != "COMPLETED":
            logger.warning(
                "Live candle gap recovery did not complete",
                event_type="MARKET_GAP_RECOVERY_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "reason": "BACKFILL_NOT_COMPLETED",
                    "backfill_status": getattr(result, "status", "UNKNOWN"),
                    "backfill_job_id": getattr(result, "job_id", None),
                },
            )
            return None

        recovered = await self._repository.list_candles(
            exchange=candle.exchange.value,
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            start_at=gap_start,
            end_at=gap_end,
            limit=missing_count,
        )
        recovered = sorted(recovered, key=lambda item: item.closed_at)
        expected_timestamps = [
            gap_start + (step * index) for index in range(missing_count)
        ]
        recovered_timestamps = [item.closed_at for item in recovered]
        if recovered_timestamps != expected_timestamps:
            logger.error(
                "Live candle gap recovery returned an incomplete sequence",
                event_type="MARKET_GAP_RECOVERY_BLOCKED",
                metadata={
                    "exchange": candle.exchange.value,
                    "symbol": candle.symbol,
                    "timeframe": candle.timeframe,
                    "reason": "RECOVERED_SEQUENCE_INCOMPLETE",
                    "expected_count": missing_count,
                    "recovered_count": len(recovered),
                },
            )
            return None

        # State hydration only. Calling the Orchestrator for these historical
        # candles would create retroactive decisions/orders, which is explicitly
        # forbidden for live continuity repair.
        for recovered_candle in recovered:
            self._store.add(recovered_candle)

        logger.info(
            "Closed-candle gap recovered before agent evaluation",
            event_type="MARKET_GAP_RECOVERED",
            metadata={
                "exchange": candle.exchange.value,
                "symbol": candle.symbol,
                "timeframe": candle.timeframe,
                "recovered_count": len(recovered),
                "backfill_job_id": getattr(result, "job_id", None),
            },
        )
        return await self._orchestrator.on_candle_closed(candle)

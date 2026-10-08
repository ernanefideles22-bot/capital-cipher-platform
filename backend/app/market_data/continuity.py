"""Live market-data continuity before agent evaluation.

A transient WebSocket interruption must not make the 300-agent cohort reason
about a candle series with holes.  This module reuses the existing historical
backfill service to repair only the missing *closed* candles, hydrates the
in-memory CandleStore, and then allows the current live candle to reach the
Orchestrator.  Recovered historical candles never generate retroactive trading
decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from app.core.logging import ServiceLogger
from app.market_data.data_quality import TIMEFRAME_SECONDS
from app.market_data.store import CandleStore
from app.schemas.backfill import HistoricalBackfillRequest
from app.schemas.market import Candle

logger = ServiceLogger("market-continuity")


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
    """Repair a detected live gap before the next current-candle decision.

    The processor is intentionally synchronous with respect to the downstream
    candle-dispatch consumer: the current candle waits for repair, while the
    Binance WebSocket receive loop remains free to keep ingesting messages into
    its bounded queue.
    """

    def __init__(
        self,
        *,
        store: CandleStore,
        repository: CandleRepository,
        backfill_service: BackfillRunner,
        orchestrator: CandleOrchestrator,
    ) -> None:
        self._store = store
        self._repository = repository
        self._backfill_service = backfill_service
        self._orchestrator = orchestrator

    async def handle(self, candle: Candle) -> Any:
        previous = self._store.latest(
            candle.exchange.value,
            candle.symbol,
            candle.timeframe,
        )
        if previous is None:
            return await self._orchestrator.on_candle_closed(candle)

        step_seconds = TIMEFRAME_SECONDS.get(candle.timeframe)
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

        # State hydration only.  Calling the Orchestrator for these historical
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

"""Fail-closed historical candle import and gap-repair workflow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from app.core.errors import CapitalCipherError, DataQualityError, DatabaseError
from app.market_data.adapters.public_rest import PublicMarketDataClient
from app.market_data.catalog import DataCatalog
from app.market_data.clock import ExchangeClockMonitor, ExchangeClockRegistry
from app.market_data.data_lake import RawDataLake
from app.market_data.data_quality import TIMEFRAME_SECONDS, evaluate_candles
from app.market_data.gaps import GapService
from app.schemas.backfill import (
    HistoricalBackfillJob,
    HistoricalBackfillRequest,
    backfill_request_fingerprint,
)
from app.schemas.common import Exchange, utcnow
from app.schemas.data_lake import BackfillQueueItem, RawProviderPage
from app.schemas.market import Candle, DataQualityReport


class BackfillRepository(Protocol):
    async def load_historical_backfill_job(
        self,
        job_id: str,
    ) -> HistoricalBackfillJob | None: ...

    async def save_historical_backfill_job(
        self,
        job: HistoricalBackfillJob,
    ) -> None: ...

    async def submit_historical_backfill(
        self,
        job: HistoricalBackfillJob,
        queue_item: BackfillQueueItem,
    ) -> bool: ...

    async def load_backfill_queue_item(
        self,
        queue_id: str,
    ) -> BackfillQueueItem | None: ...

    async def save_candles(
        self,
        candles: list[Candle],
        *,
        quality_reports: list[DataQualityReport | None] | None = None,
    ) -> int: ...


class HistoricalBackfillService:
    def __init__(
        self,
        *,
        repository: BackfillRepository,
        clients: dict[Exchange, PublicMarketDataClient],
        clock_monitor: ExchangeClockMonitor,
        clock_registry: ExchangeClockRegistry,
        gap_service: GapService,
        data_catalog: DataCatalog,
        raw_data_lake: RawDataLake | None = None,
        max_candles: int = 100_000,
    ) -> None:
        if max_candles < 1 or max_candles > 1_000_000:
            raise ValueError("max_candles must be between 1 and 1000000")
        self._repository = repository
        self._clients = clients
        self._clock_monitor = clock_monitor
        self._clock_registry = clock_registry
        self._gap_service = gap_service
        self._data_catalog = data_catalog
        self._raw_data_lake = raw_data_lake
        self._max_candles = max_candles

    async def submit(
        self,
        request: HistoricalBackfillRequest,
        *,
        max_attempts: int = 5,
    ) -> HistoricalBackfillJob:
        """Validate and atomically enqueue an idempotent historical request."""
        self._validate_request_size(request)
        if max_attempts < 1 or max_attempts > 100:
            raise ValueError("max_attempts must be between 1 and 100")
        client = self._clients.get(request.exchange)
        if client is None:
            raise ValueError(
                f"No public market-data client for {request.exchange.value}"
            )

        fingerprint = backfill_request_fingerprint(request)
        previous = await self._repository.load_historical_backfill_job(fingerprint)
        if previous is not None and previous.status == "COMPLETED":
            return previous
        if previous is not None and previous.status in {"PENDING", "RUNNING"}:
            existing_queue = await self._repository.load_backfill_queue_item(
                fingerprint
            )
            if existing_queue is not None:
                return previous

        now = utcnow()
        job = HistoricalBackfillJob(
            job_id=fingerprint,
            request_fingerprint=fingerprint,
            exchange=request.exchange,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_at=request.start_at,
            end_at=request.end_at,
            source=client.source_name,
            status="PENDING",
            attempt_count=previous.attempt_count if previous else 0,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        queue_item = BackfillQueueItem(
            queue_id=fingerprint,
            job_id=fingerprint,
            exchange=request.exchange,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_at=request.start_at,
            end_at=request.end_at,
            max_candles=request.max_candles,
            max_attempts=max_attempts,
            available_at=now,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        await self._repository.submit_historical_backfill(job, queue_item)
        return (
            await self._repository.load_historical_backfill_job(fingerprint)
            or job
        )

    async def run(
        self,
        request: HistoricalBackfillRequest,
    ) -> HistoricalBackfillJob:
        fingerprint = backfill_request_fingerprint(request)
        previous = await self._repository.load_historical_backfill_job(fingerprint)
        if previous is not None and previous.status == "COMPLETED":
            return previous

        client = self._clients[request.exchange]
        now = utcnow()
        job = HistoricalBackfillJob(
            job_id=fingerprint,
            request_fingerprint=fingerprint,
            exchange=request.exchange,
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_at=request.start_at,
            end_at=request.end_at,
            source=client.source_name,
            status="RUNNING",
            attempt_count=(previous.attempt_count if previous else 0) + 1,
            created_at=previous.created_at if previous else now,
            started_at=now,
            updated_at=now,
        )
        await self._repository.save_historical_backfill_job(job)

        try:
            self._validate_request_size(request)
            observation = await self._clock_monitor.probe(request.exchange)
            verdict = self._clock_registry.verdict(request.exchange)
            if not verdict.trusted:
                return await self._finish(
                    job,
                    status="BLOCKED",
                    clock_status=verdict.status,
                    clock_observation_id=observation.observation_id,
                    error_code="CLOCK_UNTRUSTED",
                    error_message=verdict.reason,
                )
            if request.end_at > observation.source_at:
                raise DataQualityError(
                    "Historical range must not include an open or future candle",
                    metadata={
                        "requested_end_at": request.end_at.isoformat(),
                        "source_at": observation.source_at.isoformat(),
                    },
                )

            async def archive_page(page: RawProviderPage) -> None:
                if self._raw_data_lake is not None:
                    await self._raw_data_lake.archive_page(
                        job_id=job.job_id,
                        attempt_count=job.attempt_count,
                        page=page,
                    )

            candles = await client.fetch_candles(
                symbol=request.symbol,
                timeframe=request.timeframe,
                start_at=request.start_at,
                end_at=request.end_at,
                limit=min(request.max_candles, self._max_candles),
                on_page=archive_page if self._raw_data_lake is not None else None,
            )
            self._validate_provider_batch(request, candles)
            quality = evaluate_candles(
                candles,
                timeframe=request.timeframe,
                check_staleness=False,
            )
            if quality.errors:
                raise DataQualityError(
                    "Historical provider batch failed data-quality validation",
                    metadata={"errors": quality.errors},
                )

            inserted_count = await self._repository.save_candles(
                candles,
                quality_reports=[quality] * len(candles),
            )
            remaining_gaps = await self._gap_service.scan(
                exchange=request.exchange.value,
                symbol=request.symbol,
                timeframe=request.timeframe,
                start_at=request.start_at,
                end_at=request.end_at,
                limit=min(request.max_candles, self._max_candles),
                backfill_job_id=job.job_id,
            )

            manifest = None
            if candles:
                manifest = await self._data_catalog.materialize_candle_dataset(
                    exchange=request.exchange.value,
                    symbol=request.symbol,
                    timeframe=request.timeframe,
                    start_at=request.start_at,
                    end_at=request.end_at,
                    limit=min(request.max_candles, self._max_candles),
                    clock_status=verdict.status,
                )
            return await self._finish(
                job,
                status="COMPLETED" if not remaining_gaps else "PARTIAL",
                retrieved_count=len(candles),
                inserted_count=inserted_count,
                remaining_gap_count=len(remaining_gaps),
                dataset_hash=manifest.dataset_hash if manifest else None,
                clock_status=verdict.status,
                clock_observation_id=observation.observation_id,
            )
        except DatabaseError:
            raise
        except CapitalCipherError as exc:
            return await self._finish(
                job,
                status="FAILED",
                error_code=exc.error_code,
                error_message=exc.message,
            )
        except ValueError as exc:
            return await self._finish(
                job,
                status="FAILED",
                error_code="VALIDATION_ERROR",
                error_message=str(exc),
            )
        except Exception as exc:
            return await self._finish(
                job,
                status="FAILED",
                error_code="BACKFILL_FAILED",
                error_message=f"Unexpected {type(exc).__name__}",
            )

    def _validate_request_size(self, request: HistoricalBackfillRequest) -> None:
        step_seconds = TIMEFRAME_SECONDS.get(request.timeframe)
        if step_seconds is None:
            raise ValueError(f"Unsupported timeframe: {request.timeframe}")
        expected = (
            int(
                (
                    request.end_at.astimezone(timezone.utc)
                    - request.start_at.astimezone(timezone.utc)
                ).total_seconds()
                // step_seconds
            )
            + 1
        )
        allowed = min(request.max_candles, self._max_candles)
        if expected > allowed:
            raise ValueError(
                f"Requested range contains {expected} candles; limit is {allowed}"
            )

    @staticmethod
    def _validate_provider_batch(
        request: HistoricalBackfillRequest,
        candles: list[Candle],
    ) -> None:
        timestamps = []
        for candle in candles:
            if (
                candle.exchange != request.exchange
                or candle.symbol != request.symbol
                or candle.timeframe != request.timeframe
            ):
                raise DataQualityError(
                    "Historical provider returned a different market series"
                )
            if not request.start_at <= candle.closed_at <= request.end_at:
                raise DataQualityError(
                    "Historical provider returned a candle outside the requested range"
                )
            timestamps.append(candle.closed_at)
        if timestamps != sorted(timestamps):
            raise DataQualityError("Historical provider batch is out of order")
        if len(timestamps) != len(set(timestamps)):
            raise DataQualityError("Historical provider batch contains duplicates")

    async def _finish(
        self,
        job: HistoricalBackfillJob,
        *,
        status: str,
        retrieved_count: int = 0,
        inserted_count: int = 0,
        remaining_gap_count: int = 0,
        dataset_hash: str | None = None,
        clock_observation_id: str | None = None,
        clock_status: str = "UNKNOWN",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> HistoricalBackfillJob:
        completed_at = datetime.now(timezone.utc)
        finished = job.model_copy(
            update={
                "status": status,
                "retrieved_count": retrieved_count,
                "inserted_count": inserted_count,
                "remaining_gap_count": remaining_gap_count,
                "dataset_hash": dataset_hash,
                "clock_observation_id": clock_observation_id,
                "clock_status": clock_status,
                "error_code": error_code,
                "error_message": (error_message or "")[:500] or None,
                "completed_at": completed_at,
                "updated_at": completed_at,
            }
        )
        await self._repository.save_historical_backfill_job(finished)
        return finished

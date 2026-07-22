"""Durable historical backfill worker with leases and bounded retries."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import uuid
from typing import Protocol

from app.core.errors import DatabaseError
from app.core.logging import ServiceLogger
from app.market_data.backfill import HistoricalBackfillService
from app.schemas.backfill import HistoricalBackfillJob
from app.schemas.data_lake import BackfillQueueItem

logger = ServiceLogger("backfill-worker")

_RETRYABLE_ERROR_CODES = {
    "BACKFILL_FAILED",
    "CLOCK_UNTRUSTED",
    "EXTERNAL_SERVICE_ERROR",
    "MARKET_DATA_UNAVAILABLE",
}


class BackfillQueueRepository(Protocol):
    async def claim_next_backfill(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> BackfillQueueItem | None: ...

    async def finish_backfill_queue_item(
        self,
        *,
        queue_id: str,
        worker_id: str,
        result: HistoricalBackfillJob,
        retryable: bool,
        retry_delay_seconds: float,
    ) -> BackfillQueueItem: ...


class HistoricalBackfillWorker:
    def __init__(
        self,
        *,
        repository: BackfillQueueRepository,
        service: HistoricalBackfillService,
        worker_id: str | None = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 3_600,
        retry_base_seconds: float = 5.0,
        retry_max_seconds: float = 300.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("retry delay settings are inconsistent")
        self._repository = repository
        self._service = service
        self.worker_id = worker_id or self._default_worker_id()
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    @staticmethod
    def _default_worker_id() -> str:
        identity = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        safe = re.sub(r"[^A-Za-z0-9._:-]", "-", identity)
        return f"backfill-{safe}"[:128]

    async def run_once(self) -> BackfillQueueItem | None:
        item = await self._repository.claim_next_backfill(
            worker_id=self.worker_id,
            lease_seconds=self._lease_seconds,
        )
        if item is None:
            return None

        result = await self._service.run(item.to_request())
        retryable = self._is_retryable(result)
        retry_delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** max(0, item.attempt_count - 1)),
        )
        return await self._repository.finish_backfill_queue_item(
            queue_id=item.queue_id,
            worker_id=self.worker_id,
            result=result,
            retryable=retryable,
            retry_delay_seconds=retry_delay,
        )

    @staticmethod
    def _is_retryable(result: HistoricalBackfillJob) -> bool:
        if result.status in {"PARTIAL", "BLOCKED"}:
            return True
        return (
            result.status == "FAILED"
            and result.error_code in _RETRYABLE_ERROR_CODES
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except DatabaseError as exc:
                processed = None
                logger.error(
                    "Durable backfill worker database operation failed",
                    event_type="BACKFILL_WORKER_DATABASE_ERROR",
                    metadata={"error_type": type(exc).__name__},
                )
            if processed is not None:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

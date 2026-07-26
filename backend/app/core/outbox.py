"""Retry pending journal events into the external broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import ServiceLogger
from app.core.publication import PublicationCoordinator
from app.core.transports.base import EventTransport
from app.schemas.events import BusMessage

logger = ServiceLogger("event_outbox")


class OutboxRepository(Protocol):
    async def list_pending_bus_messages(self, limit: int = 100) -> list[BusMessage]: ...

    async def is_bus_message_published(self, event_id: str) -> bool: ...

    async def mark_bus_message_published(
        self, event_id: str, broker_message_id: str
    ) -> None: ...

    async def mark_bus_message_failed(self, event_id: str, error_type: str) -> None: ...


@dataclass(frozen=True)
class OutboxDrainResult:
    attempted: int
    published: int
    failed: int


class OutboxDispatcher:
    def __init__(
        self,
        repository: OutboxRepository,
        transport: EventTransport,
        *,
        batch_size: int = 100,
        poll_interval_seconds: float = 1.0,
        publication_coordinator: PublicationCoordinator | None = None,
    ) -> None:
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._repository = repository
        self._transport = transport
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._publication_coordinator = (
            publication_coordinator or PublicationCoordinator()
        )

    async def drain_once(self) -> OutboxDrainResult:
        pending = await self._repository.list_pending_bus_messages(self._batch_size)
        results = await asyncio.gather(
            *(self._publish_one(message) for message in pending)
        )
        return OutboxDrainResult(
            attempted=len(pending),
            published=results.count("published"),
            failed=results.count("failed"),
        )

    async def _publish_one(self, message: BusMessage) -> str:
        async with self._publication_coordinator.hold(message.event_id):
            # The pending list can become stale while a direct publisher is
            # completing. Recheck under the same event lock before xadd.
            if await self._repository.is_bus_message_published(message.event_id):
                return "skipped"
            try:
                broker_id = await self._transport.publish(message)
                await self._repository.mark_bus_message_published(
                    message.event_id, broker_id
                )
                return "published"
            except Exception as exc:
                await self._repository.mark_bus_message_failed(
                    message.event_id, type(exc).__name__
                )
                logger.error(
                    "Outbox publish failed",
                    event_type="OUTBOX_PUBLISH_FAILED",
                    correlation_id=message.correlation_id,
                    metadata={
                        "event_id": message.event_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return "failed"

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.drain_once()
            except Exception as exc:
                logger.error(
                    "Outbox polling failed",
                    event_type="OUTBOX_POLL_FAILED",
                    metadata={"error_type": type(exc).__name__},
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                continue

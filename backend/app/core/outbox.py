"""Retry pending journal events into the external broker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import ServiceLogger
from app.core.publication import PublicationCoordinator
from app.core.transports.base import EventTransport
from app.database.repositories.outbox_batch import (
    list_pending_bus_messages as durable_pending_bus_messages,
    list_published_bus_message_ids as durable_published_bus_message_ids,
    list_untracked_bus_messages as durable_untracked_bus_messages,
)
from app.schemas.events import BusMessage

logger = ServiceLogger("event_outbox")


class OutboxRepository(Protocol):
    async def list_pending_bus_messages(self, limit: int = 100) -> list[BusMessage]: ...

    async def is_bus_message_published(self, event_id: str) -> bool: ...

    async def mark_bus_message_published(
        self, event_id: str, broker_message_id: str
    ) -> None: ...

    async def mark_bus_messages_published(
        self,
        published: list[tuple[str, str]],
    ) -> None: ...

    async def mark_bus_message_failed(self, event_id: str, error_type: str) -> None: ...

    async def mark_bus_messages_failed(
        self,
        event_ids: list[str],
        error_type: str,
    ) -> None: ...


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
        # Normal polling follows the partial event_outbox index. A separate,
        # infrequent cardinality/anti-join check preserves recovery from the
        # narrow crash window between journaling and recording broker state.
        self._next_untracked_scan_at = 0.0
        self._untracked_scan_interval_seconds = 60.0

    async def _pending_messages(self) -> list[BusMessage]:
        now = time.monotonic()
        if now >= self._next_untracked_scan_at:
            untracked = await durable_untracked_bus_messages(
                self._repository,
                self._batch_size,
            )
            if untracked is not None:
                if untracked:
                    # Keep scanning crash-gap rows on successive drains until
                    # the journal and outbox cardinalities converge.
                    self._next_untracked_scan_at = 0.0
                    return untracked
                self._next_untracked_scan_at = (
                    now + self._untracked_scan_interval_seconds
                )
            else:
                self._next_untracked_scan_at = (
                    now + self._untracked_scan_interval_seconds
                )

        durable_pending = await durable_pending_bus_messages(
            self._repository,
            self._batch_size,
        )
        if durable_pending is not None:
            return durable_pending
        return await self._repository.list_pending_bus_messages(
            self._batch_size
        )

    async def _published_ids(self, event_ids: list[str]) -> set[str]:
        """Recheck publication state with one query when batch support exists."""

        batch_method = getattr(
            self._repository,
            "list_published_bus_message_ids",
            None,
        )
        if batch_method is not None:
            return set(await batch_method(event_ids))
        durable_ids = await durable_published_bus_message_ids(
            self._repository,
            event_ids,
        )
        if durable_ids is not None:
            return durable_ids
        states = await asyncio.gather(
            *(
                self._repository.is_bus_message_published(event_id)
                for event_id in event_ids
            )
        )
        return {
            event_id
            for event_id, published in zip(event_ids, states, strict=True)
            if published
        }

    async def _mark_published(
        self,
        published: list[tuple[str, str]],
    ) -> None:
        batch_method = getattr(
            self._repository,
            "mark_bus_messages_published",
            None,
        )
        if batch_method is not None:
            await batch_method(published)
            return
        await asyncio.gather(
            *(
                self._repository.mark_bus_message_published(event_id, broker_id)
                for event_id, broker_id in published
            )
        )

    async def _mark_failed(
        self,
        event_ids: list[str],
        error_type: str,
    ) -> None:
        batch_method = getattr(
            self._repository,
            "mark_bus_messages_failed",
            None,
        )
        if batch_method is not None:
            await batch_method(event_ids, error_type)
            return
        await asyncio.gather(
            *(
                self._repository.mark_bus_message_failed(event_id, error_type)
                for event_id in event_ids
            )
        )

    async def drain_once(self) -> OutboxDrainResult:
        """Replay one cohort without one DB/broker round-trip per event.

        Direct publication and recovery share the same per-event coordinator.
        The cohort lock prevents an outbox replay racing a direct publisher;
        publication state is then rechecked once for the whole cohort. Redis
        Streams remains at-least-once and PostgreSQL remains the durable audit
        source.
        """

        pending = await self._pending_messages()
        if not pending:
            return OutboxDrainResult(attempted=0, published=0, failed=0)

        event_ids = [message.event_id for message in pending]
        async with self._publication_coordinator.hold_many(event_ids):
            published_ids = await self._published_ids(event_ids)
            replay = [
                message
                for message in pending
                if message.event_id not in published_ids
            ]
            if not replay:
                return OutboxDrainResult(
                    attempted=len(pending),
                    published=0,
                    failed=0,
                )

            try:
                publish_many = getattr(self._transport, "publish_many", None)
                if publish_many is not None:
                    broker_ids = await publish_many(replay)
                else:
                    broker_ids = await asyncio.gather(
                        *(self._transport.publish(message) for message in replay)
                    )
                if len(broker_ids) != len(replay):
                    raise RuntimeError(
                        "Broker batch result length mismatch"
                    )
                published = [
                    (message.event_id, broker_id)
                    for message, broker_id in zip(
                        replay,
                        broker_ids,
                        strict=True,
                    )
                ]
                await self._mark_published(published)
                return OutboxDrainResult(
                    attempted=len(pending),
                    published=len(replay),
                    failed=0,
                )
            except Exception as exc:
                replay_ids = [message.event_id for message in replay]
                await self._mark_failed(replay_ids, type(exc).__name__)
                logger.error(
                    "Outbox batch publish failed",
                    event_type="OUTBOX_PUBLISH_FAILED",
                    metadata={
                        "event_count": len(replay_ids),
                        "error_type": type(exc).__name__,
                    },
                )
                return OutboxDrainResult(
                    attempted=len(pending),
                    published=0,
                    failed=len(replay),
                )

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

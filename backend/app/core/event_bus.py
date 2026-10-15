"""In-memory message bus (docs/23-message-bus.md, ADR-002, ADR-006).

Phase 1 implementation: asyncio-based in-memory dispatcher with topics,
correlation_id preservation and failure logging. The interface is designed so
Redis Streams / RabbitMQ / NATS / Kafka can replace it in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.journal import JournalWriteResult
from app.core.logging import ServiceLogger
from app.core.payload_security import ensure_payload_has_no_secrets
from app.core.publication import PublicationCoordinator
from app.core.transports.base import EventTransport
from app.schemas.events import BusMessage

Handler = Callable[[BusMessage], Awaitable[None]]
EventJournal = Callable[
    [BusMessage], Awaitable[bool | JournalWriteResult | None]
]
EventBatchJournal = Callable[
    [list[BusMessage]], Awaitable[dict[str, JournalWriteResult]]
]
EventPublishMarker = Callable[[str, str], Awaitable[None]]
EventBatchPublishMarker = Callable[
    [list[tuple[str, str]]], Awaitable[None]
]
EventFailureMarker = Callable[[str, str], Awaitable[None]]
EventBatchFailureMarker = Callable[[list[str], str], Awaitable[None]]

logger = ServiceLogger("message_bus")


@dataclass(frozen=True)
class EventPublication:
    topic: str
    event_type: str
    payload: dict[str, Any]
    source: str
    correlation_id: str
    event_id: str


class Topics:
    RAW_MARKET_EVENTS = "market.raw.v1"
    MARKET_EVENTS = "market.events.v1"
    AGENT_REQUESTS = "agent.requests.v1"
    AGENT_OUTPUTS = "agent.outputs.v1"
    DECISION_EVENTS = "decision.events.v1"
    RISK_EVENTS = "risk.events.v1"
    PAPER_ORDERS = "paper.orders.v1"
    OMS_ORDERS = "oms.orders.v1"
    AUDIT_EVENTS = "audit.events.v1"
    SYSTEM_EVENTS = "system.events.v1"
    DEAD_LETTER = "dead_letter.events.v1"


class EventBus:
    """Deterministic async pub/sub bus with journaling and bounded deduplication.

    The interface remains broker-neutral so a durable external transport can
    replace this in-process implementation without changing producers.
    """

    def __init__(
        self,
        *,
        journal: EventJournal | None = None,
        journal_many: EventBatchJournal | None = None,
        transport: EventTransport | None = None,
        transport_required: bool = False,
        mark_published: EventPublishMarker | None = None,
        mark_published_many: EventBatchPublishMarker | None = None,
        mark_failed: EventFailureMarker | None = None,
        mark_failed_many: EventBatchFailureMarker | None = None,
        publication_coordinator: PublicationCoordinator | None = None,
        max_processed_event_ids: int = 100_000,
    ) -> None:
        if max_processed_event_ids < 1:
            raise ValueError("max_processed_event_ids must be positive")
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._journal = journal
        self._journal_many = journal_many
        self._transport = transport
        self._transport_required = transport_required
        self._mark_published = mark_published
        self._mark_published_many = mark_published_many
        self._mark_failed = mark_failed
        self._mark_failed_many = mark_failed_many
        self._publication_coordinator = (
            publication_coordinator or PublicationCoordinator()
        )
        self._max_processed_event_ids = max_processed_event_ids
        self._processed_event_ids: OrderedDict[str, None] = OrderedDict()

    def subscribe(self, topic: str, handler: Handler) -> None:
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        subscribers = self._subscribers.get(topic)
        if subscribers and handler in subscribers:
            subscribers.remove(handler)

    def _remember(self, event_id: str) -> None:
        self._processed_event_ids[event_id] = None
        self._processed_event_ids.move_to_end(event_id)
        while len(self._processed_event_ids) > self._max_processed_event_ids:
            self._processed_event_ids.popitem(last=False)

    @staticmethod
    def _message(publication: EventPublication) -> BusMessage:
        message = BusMessage(
            message_id=str(uuid4()),
            event_id=publication.event_id,
            correlation_id=publication.correlation_id,
            topic=publication.topic,
            event_type=publication.event_type,
            source=publication.source,
            payload=publication.payload,
        )
        ensure_payload_has_no_secrets(message.payload)
        return message

    async def publish(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        source: str,
        correlation_id: str | None = None,
        event_id: str | None = None,
    ) -> BusMessage:
        if correlation_id is None:
            raise ValueError("correlation_id is mandatory on the message bus (docs/23).")
        message = self._message(
            EventPublication(
                topic=topic,
                event_type=event_type,
                payload=payload,
                source=source,
                correlation_id=correlation_id,
                event_id=event_id or str(uuid4()),
            )
        )
        # Idempotency guard for critical events (docs/23).
        if message.event_id in self._processed_event_ids:
            logger.warning(
                "Duplicate event ignored",
                event_type="DUPLICATE_EVENT",
                correlation_id=correlation_id,
                metadata={"event_id": message.event_id, "topic": topic},
            )
            return message

        # Direct publication and the outbox share a per-event lock. This keeps
        # one event idempotent without serializing unrelated agent events.
        async with self._publication_coordinator.hold(message.event_id):
            if message.event_id in self._processed_event_ids:
                logger.warning(
                    "Duplicate event ignored",
                    event_type="DUPLICATE_EVENT",
                    correlation_id=correlation_id,
                    metadata={"event_id": message.event_id, "topic": topic},
                )
                return message
            # Durable journal is written before in-process delivery. A journal
            # failure stops the event so downstream consumers never act on an
            # event that cannot be replayed or audited.
            if self._journal is not None:
                journaled = await self._journal(message)
                if journaled is False:
                    self._remember(message.event_id)
                    logger.warning(
                        "Duplicate durable event ignored",
                        event_type="DUPLICATE_EVENT",
                        correlation_id=correlation_id,
                        metadata={"event_id": message.event_id, "topic": topic},
                    )
                    return message
                if (
                    isinstance(journaled, JournalWriteResult)
                    and not journaled.inserted
                    and (self._transport is None or journaled.broker_published)
                ):
                    self._remember(message.event_id)
                    logger.warning(
                        "Duplicate durable event ignored",
                        event_type="DUPLICATE_EVENT",
                        correlation_id=correlation_id,
                        metadata={"event_id": message.event_id, "topic": topic},
                    )
                    return message

            if self._transport is not None:
                try:
                    broker_id = await self._transport.publish(message)
                    if self._mark_published is not None:
                        await self._mark_published(message.event_id, broker_id)
                except Exception as exc:
                    if self._mark_failed is not None:
                        try:
                            await self._mark_failed(message.event_id, type(exc).__name__)
                        except Exception:
                            logging.getLogger("message_bus").exception(
                                "Failed to record broker publish failure"
                            )
                    if self._transport_required:
                        raise
                    logger.error(
                        "Optional broker publish failed; event remains in durable outbox",
                        event_type="BROKER_PUBLISH_FAILED",
                        correlation_id=correlation_id,
                        metadata={
                            "event_id": message.event_id,
                            "error_type": type(exc).__name__,
                        },
                    )
            self._remember(message.event_id)

        for handler in self._subscribers.get(topic, []):
            try:
                await handler(message)
            except Exception:
                logger.error(
                    f"Handler failed for topic {topic}",
                    event_type="BUS_HANDLER_FAILED",
                    correlation_id=correlation_id,
                    metadata={"topic": topic, "event_type_failed": event_type},
                    exc_info=True,
                )
                await self._to_dead_letter(message)
        return message

    async def publish_many(
        self,
        publications: list[EventPublication],
    ) -> list[BusMessage]:
        if not publications:
            return []
        messages = [self._message(publication) for publication in publications]
        event_ids = [message.event_id for message in messages]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Batch event_ids must be unique")

        deliver: list[BusMessage] = []
        active = [
            message
            for message in messages
            if message.event_id not in self._processed_event_ids
        ]
        if not active:
            return messages

        async with self._publication_coordinator.hold_many(
            [message.event_id for message in active]
        ):
            active = [
                message
                for message in active
                if message.event_id not in self._processed_event_ids
            ]
            if not active:
                return messages

            journal_results: dict[
                str, bool | JournalWriteResult | None
            ] = {}
            if self._journal_many is not None:
                journal_results = await self._journal_many(active)
            elif self._journal is not None:
                results = await asyncio.gather(
                    *(self._journal(message) for message in active)
                )
                journal_results = {
                    message.event_id: result
                    for message, result in zip(active, results, strict=True)
                }

            to_transport: list[BusMessage] = []
            for message in active:
                journaled = journal_results.get(message.event_id)
                if journaled is False:
                    self._remember(message.event_id)
                    continue
                if (
                    isinstance(journaled, JournalWriteResult)
                    and not journaled.inserted
                    and (
                        self._transport is None
                        or journaled.broker_published
                    )
                ):
                    self._remember(message.event_id)
                    continue
                to_transport.append(message)
                deliver.append(message)

            if self._transport is not None and to_transport:
                try:
                    publish_many = getattr(
                        self._transport,
                        "publish_many",
                        None,
                    )
                    if publish_many is None:
                        broker_ids = await asyncio.gather(
                            *(
                                self._transport.publish(message)
                                for message in to_transport
                            )
                        )
                    else:
                        broker_ids = await publish_many(to_transport)
                    if len(broker_ids) != len(to_transport):
                        raise RuntimeError(
                            "Broker batch result length mismatch"
                        )
                    published = [
                        (message.event_id, broker_id)
                        for message, broker_id in zip(
                            to_transport,
                            broker_ids,
                            strict=True,
                        )
                    ]
                    if self._mark_published_many is not None:
                        await self._mark_published_many(published)
                    elif self._mark_published is not None:
                        await asyncio.gather(
                            *(
                                self._mark_published(event_id, broker_id)
                                for event_id, broker_id in published
                            )
                        )
                except Exception as exc:
                    event_ids = [
                        message.event_id for message in to_transport
                    ]
                    try:
                        if self._mark_failed_many is not None:
                            await self._mark_failed_many(
                                event_ids,
                                type(exc).__name__,
                            )
                        elif self._mark_failed is not None:
                            await asyncio.gather(
                                *(
                                    self._mark_failed(
                                        event_id,
                                        type(exc).__name__,
                                    )
                                    for event_id in event_ids
                                )
                            )
                    except Exception:
                        logging.getLogger("message_bus").exception(
                            "Failed to record broker batch publish failure"
                        )
                    if self._transport_required:
                        raise
                    logger.error(
                        "Optional broker batch failed; events remain in outbox",
                        event_type="BROKER_BATCH_PUBLISH_FAILED",
                        correlation_id=to_transport[0].correlation_id,
                        metadata={
                            "event_count": len(to_transport),
                            "error_type": type(exc).__name__,
                        },
                    )

            for message in deliver:
                self._remember(message.event_id)

        for message in deliver:
            for handler in self._subscribers.get(message.topic, []):
                try:
                    await handler(message)
                except Exception:
                    logger.error(
                        f"Handler failed for topic {message.topic}",
                        event_type="BUS_HANDLER_FAILED",
                        correlation_id=message.correlation_id,
                        metadata={
                            "topic": message.topic,
                            "event_type_failed": message.event_type,
                        },
                        exc_info=True,
                    )
                    await self._to_dead_letter(message)
        return messages

    async def _to_dead_letter(self, message: BusMessage) -> None:
        for handler in self._subscribers.get(Topics.DEAD_LETTER, []):
            try:
                await handler(message)
            except Exception:
                logging.getLogger("message_bus").exception("Dead letter handler failed")

"""In-memory message bus (docs/23-message-bus.md, ADR-002, ADR-006).

Phase 1 implementation: asyncio-based in-memory dispatcher with topics,
correlation_id preservation and failure logging. The interface is designed so
Redis Streams / RabbitMQ / NATS / Kafka can replace it in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, defaultdict
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.journal import JournalWriteResult
from app.core.logging import ServiceLogger
from app.core.payload_security import ensure_payload_has_no_secrets
from app.core.transports.base import EventTransport
from app.schemas.events import BusMessage

Handler = Callable[[BusMessage], Awaitable[None]]
EventJournal = Callable[
    [BusMessage], Awaitable[bool | JournalWriteResult | None]
]
EventPublishMarker = Callable[[str, str], Awaitable[None]]
EventFailureMarker = Callable[[str, str], Awaitable[None]]

logger = ServiceLogger("message_bus")


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
        transport: EventTransport | None = None,
        transport_required: bool = False,
        mark_published: EventPublishMarker | None = None,
        mark_failed: EventFailureMarker | None = None,
        publication_lock: asyncio.Lock | None = None,
        max_processed_event_ids: int = 100_000,
    ) -> None:
        if max_processed_event_ids < 1:
            raise ValueError("max_processed_event_ids must be positive")
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._journal = journal
        self._transport = transport
        self._transport_required = transport_required
        self._mark_published = mark_published
        self._mark_failed = mark_failed
        self._publication_lock = publication_lock or asyncio.Lock()
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
        message = BusMessage(
            message_id=str(uuid4()),
            event_id=event_id or str(uuid4()),
            correlation_id=correlation_id,
            topic=topic,
            event_type=event_type,
            source=source,
            payload=payload,
        )
        ensure_payload_has_no_secrets(message.payload)
        # Idempotency guard for critical events (docs/23).
        if message.event_id in self._processed_event_ids:
            logger.warning(
                "Duplicate event ignored",
                event_type="DUPLICATE_EVENT",
                correlation_id=correlation_id,
                metadata={"event_id": message.event_id, "topic": topic},
            )
            return message

        # The shared lock keeps the direct publisher and the single-process
        # outbox dispatcher from racing on the same journal row.
        async with self._publication_lock:
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

    async def _to_dead_letter(self, message: BusMessage) -> None:
        for handler in self._subscribers.get(Topics.DEAD_LETTER, []):
            try:
                await handler(message)
            except Exception:
                logging.getLogger("message_bus").exception("Dead letter handler failed")

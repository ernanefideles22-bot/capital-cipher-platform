"""In-memory message bus (docs/23-message-bus.md, ADR-002, ADR-006).

Phase 1 implementation: asyncio-based in-memory dispatcher with topics,
correlation_id preservation and failure logging. The interface is designed so
Redis Streams / RabbitMQ / NATS / Kafka can replace it in later phases.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.logging import ServiceLogger
from app.schemas.events import BusMessage

Handler = Callable[[BusMessage], Awaitable[None]]

logger = ServiceLogger("message_bus")


class Topics:
    MARKET_EVENTS = "market.events"
    AGENT_REQUESTS = "agent.requests"
    AGENT_OUTPUTS = "agent.outputs"
    DECISION_EVENTS = "decision.events"
    RISK_EVENTS = "risk.events"
    PAPER_ORDERS = "paper.orders"
    AUDIT_EVENTS = "audit.events"
    SYSTEM_EVENTS = "system.events"
    DEAD_LETTER = "dead_letter.events"


class EventBus:
    """Simple async pub/sub bus with per-topic subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._processed_message_ids: set[str] = set()

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subscribers[topic].append(handler)

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
        # Idempotency guard for critical events (docs/23).
        if message.event_id in self._processed_message_ids:
            logger.warning(
                "Duplicate event ignored",
                event_type="DUPLICATE_EVENT",
                correlation_id=correlation_id,
                metadata={"event_id": message.event_id, "topic": topic},
            )
            return message
        self._processed_message_ids.add(message.event_id)

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

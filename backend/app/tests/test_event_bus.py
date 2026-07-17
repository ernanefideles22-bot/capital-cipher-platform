"""Versioned event bus, journal, and delivery semantics."""

from __future__ import annotations

from app.core.event_bus import EventBus, Topics


async def test_journal_happens_before_delivery():
    call_order: list[str] = []

    async def journal(message):
        assert message.schema_version == "1.0.0"
        call_order.append("journal")

    async def handler(message):
        assert message.topic == Topics.SYSTEM_EVENTS
        call_order.append("handler")

    bus = EventBus(journal=journal)
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STARTED",
        {},
        source="test",
        correlation_id="correlation-1",
    )

    assert call_order == ["journal", "handler"]


async def test_duplicate_event_is_delivered_only_once():
    received = []

    async def handler(message):
        received.append(message.event_id)

    bus = EventBus(max_processed_event_ids=10)
    bus.subscribe(Topics.MARKET_EVENTS, handler)
    for _ in range(2):
        await bus.publish(
            Topics.MARKET_EVENTS,
            "CANDLE_CLOSED",
            {},
            source="test",
            correlation_id="correlation-2",
            event_id="stable-event-id",
        )

    assert received == ["stable-event-id"]


async def test_duplicate_subscription_does_not_duplicate_delivery():
    received = []

    async def handler(message):
        received.append(message.message_id)

    bus = EventBus()
    bus.subscribe(Topics.AUDIT_EVENTS, handler)
    bus.subscribe(Topics.AUDIT_EVENTS, handler)
    await bus.publish(
        Topics.AUDIT_EVENTS,
        "AUDIT_LOG_CREATED",
        {},
        source="test",
        correlation_id="correlation-3",
    )

    assert len(received) == 1


def test_deduplication_window_must_be_positive():
    try:
        EventBus(max_processed_event_ids=0)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("invalid deduplication window was accepted")

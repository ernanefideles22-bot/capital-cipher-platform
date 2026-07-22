"""Redis Streams transport and outbox delivery semantics."""

from __future__ import annotations

import asyncio

import pytest

from app.core.event_bus import EventBus, Topics
from app.core.journal import JournalWriteResult
from app.core.outbox import OutboxDispatcher
from app.core.transports.redis_streams import RedisStreamTransport
from app.schemas.events import BusMessage


class FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []
        self.closed = False
        self.read_response = []

    async def ping(self):
        return True

    async def xadd(self, stream, fields, **kwargs):
        self.entries.append((stream, fields))
        return f"1000-{len(self.entries)}"

    async def xread(self, streams, **kwargs):
        return self.read_response

    async def aclose(self):
        self.closed = True


def make_message(**overrides) -> BusMessage:
    values = {
        "event_id": "event-1",
        "correlation_id": "correlation-1",
        "topic": Topics.SYSTEM_EVENTS,
        "event_type": "SYSTEM_STARTED",
        "source": "test",
        "payload": {"mode": "PAPER"},
    }
    values.update(overrides)
    return BusMessage(**values)


async def test_redis_transport_round_trip_and_stream_limits():
    redis = FakeRedis()
    transport = RedisStreamTransport(
        "redis://unused",
        stream_prefix="capital-cipher-test",
        max_stream_length=500,
        client=redis,
    )
    message = make_message()

    stream_id = await transport.publish(message)
    stream, fields = redis.entries[0]
    redis.read_response = [(stream, [(stream_id, fields)])]
    records = await transport.read_after(Topics.SYSTEM_EVENTS)

    assert stream == "capital-cipher-test:system.events.v1"
    assert stream_id == "1000-1"
    assert records[0].message == message
    assert records[0].stream_id == stream_id
    assert await transport.healthcheck() is True
    await transport.close()
    assert redis.closed is True


async def test_redis_transport_rejects_sensitive_payload_fields():
    transport = RedisStreamTransport("redis://unused", client=FakeRedis())
    with pytest.raises(ValueError, match="Sensitive field"):
        await transport.publish(
            make_message(payload={"nested": {"api_key": "must-not-leak"}})
        )


async def test_event_bus_rejects_secret_before_journaling():
    journaled: list[str] = []

    async def journal(message):
        journaled.append(message.event_id)

    bus = EventBus(journal=journal)
    with pytest.raises(ValueError, match="api_secret"):
        await bus.publish(
            Topics.SYSTEM_EVENTS,
            "SYSTEM_STARTED",
            {"nested": [{"api_secret": "must-not-be-stored"}]},
            source="test",
            correlation_id="correlation-secret",
            event_id="event-secret",
        )

    assert journaled == []


async def test_event_bus_orders_journal_broker_marker_and_local_delivery():
    call_order: list[str] = []

    async def journal(message):
        call_order.append("journal")
        return JournalWriteResult(inserted=True, broker_published=False)

    class Transport:
        async def publish(self, message):
            call_order.append("broker")
            return "2000-1"

    async def mark_published(event_id, broker_id):
        assert (event_id, broker_id) == ("event-order", "2000-1")
        call_order.append("marker")

    async def handler(message):
        call_order.append("handler")

    bus = EventBus(
        journal=journal,
        transport=Transport(),
        mark_published=mark_published,
    )
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STARTED",
        {},
        source="test",
        correlation_id="correlation-order",
        event_id="event-order",
    )

    assert call_order == ["journal", "broker", "marker", "handler"]


async def test_optional_broker_failure_keeps_paper_delivery_and_marks_outbox():
    delivered: list[str] = []
    failed: list[tuple[str, str]] = []

    async def journal(message):
        return JournalWriteResult(inserted=True, broker_published=False)

    class FailingTransport:
        async def publish(self, message):
            raise ConnectionError("broker unavailable")

    async def mark_failed(event_id, error_type):
        failed.append((event_id, error_type))

    async def handler(message):
        delivered.append(message.event_id)

    bus = EventBus(
        journal=journal,
        transport=FailingTransport(),
        transport_required=False,
        mark_failed=mark_failed,
    )
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STARTED",
        {},
        source="test",
        correlation_id="correlation-optional",
        event_id="event-optional",
    )

    assert delivered == ["event-optional"]
    assert failed == [("event-optional", "ConnectionError")]


async def test_required_broker_failure_blocks_local_delivery():
    delivered: list[str] = []

    async def journal(message):
        return JournalWriteResult(inserted=True, broker_published=False)

    class FailingTransport:
        async def publish(self, message):
            raise ConnectionError("broker unavailable")

    async def handler(message):
        delivered.append(message.event_id)

    bus = EventBus(
        journal=journal,
        transport=FailingTransport(),
        transport_required=True,
    )
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    with pytest.raises(ConnectionError):
        await bus.publish(
            Topics.SYSTEM_EVENTS,
            "SYSTEM_STARTED",
            {},
            source="test",
            correlation_id="correlation-required",
            event_id="event-required",
        )

    assert delivered == []


async def test_outbox_retries_pending_messages():
    message = make_message(event_id="pending-event")

    class Repository:
        def __init__(self):
            self.pending = [message]
            self.published = []
            self.failed = []

        async def list_pending_bus_messages(self, limit=100):
            return self.pending[:limit]

        async def mark_bus_message_published(self, event_id, broker_id):
            self.published.append((event_id, broker_id))
            self.pending = []

        async def mark_bus_message_failed(self, event_id, error_type):
            self.failed.append((event_id, error_type))

    class Transport:
        async def publish(self, pending):
            return "3000-1"

    repository = Repository()
    result = await OutboxDispatcher(repository, Transport()).drain_once()

    assert result.attempted == 1
    assert result.published == 1
    assert result.failed == 0
    assert repository.published == [("pending-event", "3000-1")]


async def test_direct_publish_and_outbox_do_not_race_in_one_process():
    shared_lock = asyncio.Lock()
    journal_written = asyncio.Event()
    pending: list[BusMessage] = []
    published: list[tuple[str, str]] = []

    class Repository:
        async def save_bus_message(self, message):
            pending.append(message)
            journal_written.set()
            await asyncio.sleep(0)
            return JournalWriteResult(inserted=True, broker_published=False)

        async def list_pending_bus_messages(self, limit=100):
            return pending[:limit]

        async def mark_bus_message_published(self, event_id, broker_id):
            published.append((event_id, broker_id))
            pending[:] = [
                message for message in pending if message.event_id != event_id
            ]

        async def mark_bus_message_failed(self, event_id, error_type):
            raise AssertionError("transport should not fail")

    class Transport:
        def __init__(self):
            self.calls = 0

        async def publish(self, message):
            self.calls += 1
            return f"4000-{self.calls}"

    repository = Repository()
    transport = Transport()
    bus = EventBus(
        journal=repository.save_bus_message,
        transport=transport,
        mark_published=repository.mark_bus_message_published,
        publication_lock=shared_lock,
    )
    dispatcher = OutboxDispatcher(
        repository,
        transport,
        publication_lock=shared_lock,
    )

    direct_task = asyncio.create_task(
        bus.publish(
            Topics.SYSTEM_EVENTS,
            "SYSTEM_STARTED",
            {},
            source="test",
            correlation_id="correlation-race",
            event_id="event-race",
        )
    )
    await journal_written.wait()
    drain_result = await dispatcher.drain_once()
    await direct_task

    assert transport.calls == 1
    assert published == [("event-race", "4000-1")]
    assert drain_result.attempted == 0

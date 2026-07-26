"""Redis Streams transport and outbox delivery semantics."""

from __future__ import annotations

import asyncio

import pytest

from app.core.event_bus import EventBus, EventPublication, Topics
from app.core.journal import JournalWriteResult
from app.core.outbox import OutboxDispatcher
from app.core.publication import PublicationCoordinator
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

    def pipeline(self, **kwargs):
        assert kwargs == {"transaction": False}
        return FakeRedisPipeline(self)


class FakeRedisPipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def xadd(self, stream, fields, **kwargs):
        self.commands.append((stream, fields, kwargs))
        return self

    async def execute(self):
        return [
            await self.redis.xadd(stream, fields, **kwargs)
            for stream, fields, kwargs in self.commands
        ]


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


async def test_redis_transport_pipelines_event_cohort_in_order():
    redis = FakeRedis()
    transport = RedisStreamTransport(
        "redis://unused",
        stream_prefix="capital-cipher-test",
        client=redis,
    )
    messages = [
        make_message(
            event_id=f"event-{index}",
            correlation_id=f"correlation-{index}",
        )
        for index in range(3)
    ]

    stream_ids = await transport.publish_many(messages)

    assert stream_ids == ["1000-1", "1000-2", "1000-3"]
    assert len(redis.entries) == 3
    assert [
        BusMessage.model_validate_json(fields["message"]).event_id
        for _, fields in redis.entries
    ] == ["event-0", "event-1", "event-2"]


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

        async def is_bus_message_published(self, event_id):
            return any(item[0] == event_id for item in self.published)

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
    coordinator = PublicationCoordinator(max_concurrency=4)
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

        async def is_bus_message_published(self, event_id):
            return any(item[0] == event_id for item in published)

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
        publication_coordinator=coordinator,
    )
    dispatcher = OutboxDispatcher(
        repository,
        transport,
        publication_coordinator=coordinator,
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
    assert drain_result.attempted == 1
    assert drain_result.published == 0
    assert drain_result.failed == 0


async def test_event_publication_is_bounded_but_not_globally_serialized():
    active = 0
    max_active = 0

    async def journal(message):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return JournalWriteResult(inserted=True, broker_published=False)

    coordinator = PublicationCoordinator(max_concurrency=3)
    bus = EventBus(
        journal=journal,
        publication_coordinator=coordinator,
    )

    await asyncio.gather(
        *(
            bus.publish(
                Topics.SYSTEM_EVENTS,
                "SYSTEM_STARTED",
                {},
                source="test",
                correlation_id=f"correlation-{index}",
                event_id=f"event-{index}",
            )
            for index in range(9)
        )
    )

    assert max_active == 3


async def test_concurrent_duplicate_event_is_published_once_without_journal():
    class Transport:
        def __init__(self):
            self.calls = 0

        async def publish(self, message):
            self.calls += 1
            await asyncio.sleep(0.01)
            return f"5000-{self.calls}"

    transport = Transport()
    bus = EventBus(
        transport=transport,
        publication_coordinator=PublicationCoordinator(max_concurrency=4),
    )

    await asyncio.gather(
        *(
            bus.publish(
                Topics.SYSTEM_EVENTS,
                "SYSTEM_STARTED",
                {},
                source="test",
                correlation_id="correlation-duplicate",
                event_id="event-duplicate",
            )
            for _ in range(4)
        )
    )

    assert transport.calls == 1


async def test_event_bus_batches_journal_broker_marker_and_delivery():
    call_order: list[str] = []

    async def journal_many(messages):
        call_order.append("journal_many")
        return {
            message.event_id: JournalWriteResult(
                inserted=True,
                broker_published=False,
            )
            for message in messages
        }

    class Transport:
        async def publish_many(self, messages):
            call_order.append("broker_many")
            return [
                f"6000-{index}"
                for index, _ in enumerate(messages, start=1)
            ]

    async def mark_published_many(published):
        assert published == [
            ("batch-event-1", "6000-1"),
            ("batch-event-2", "6000-2"),
        ]
        call_order.append("marker_many")

    async def handler(message):
        call_order.append(f"handler:{message.event_id}")

    bus = EventBus(
        journal_many=journal_many,
        transport=Transport(),
        mark_published_many=mark_published_many,
    )
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    publications = [
        EventPublication(
            topic=Topics.SYSTEM_EVENTS,
            event_type="SYSTEM_STARTED",
            payload={"index": index},
            source="test",
            correlation_id=f"batch-correlation-{index}",
            event_id=f"batch-event-{index}",
        )
        for index in range(1, 3)
    ]

    await bus.publish_many(publications)

    assert call_order == [
        "journal_many",
        "broker_many",
        "marker_many",
        "handler:batch-event-1",
        "handler:batch-event-2",
    ]


async def test_concurrent_duplicate_batches_publish_each_event_once():
    class Transport:
        def __init__(self):
            self.event_ids: list[str] = []

        async def publish_many(self, messages):
            self.event_ids.extend(
                message.event_id for message in messages
            )
            await asyncio.sleep(0.01)
            return [
                f"7000-{index}"
                for index, _ in enumerate(messages, start=1)
            ]

    publications = [
        EventPublication(
            topic=Topics.SYSTEM_EVENTS,
            event_type="SYSTEM_STARTED",
            payload={},
            source="test",
            correlation_id="batch-duplicate-correlation",
            event_id=f"batch-duplicate-{index}",
        )
        for index in range(3)
    ]
    transport = Transport()
    bus = EventBus(
        transport=transport,
        publication_coordinator=PublicationCoordinator(
            max_concurrency=4
        ),
    )

    await asyncio.gather(
        bus.publish_many(publications),
        bus.publish_many(publications),
    )

    assert transport.event_ids == [
        "batch-duplicate-0",
        "batch-duplicate-1",
        "batch-duplicate-2",
    ]


async def test_required_batch_broker_failure_blocks_local_delivery():
    delivered: list[str] = []
    failed: list[tuple[list[str], str]] = []

    async def journal_many(messages):
        return {
            message.event_id: JournalWriteResult(
                inserted=True,
                broker_published=False,
            )
            for message in messages
        }

    class FailingTransport:
        async def publish_many(self, messages):
            raise ConnectionError("broker unavailable")

    async def mark_failed_many(event_ids, error_type):
        failed.append((event_ids, error_type))

    async def handler(message):
        delivered.append(message.event_id)

    bus = EventBus(
        journal_many=journal_many,
        transport=FailingTransport(),
        transport_required=True,
        mark_failed_many=mark_failed_many,
    )
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)
    publications = [
        EventPublication(
            topic=Topics.SYSTEM_EVENTS,
            event_type="SYSTEM_STARTED",
            payload={},
            source="test",
            correlation_id="batch-failure-correlation",
            event_id=f"batch-failure-{index}",
        )
        for index in range(2)
    ]

    with pytest.raises(ConnectionError):
        await bus.publish_many(publications)

    assert delivered == []
    assert failed == [
        (
            ["batch-failure-0", "batch-failure-1"],
            "ConnectionError",
        )
    ]

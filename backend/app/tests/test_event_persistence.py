"""Persistence tests for replayable events and raw public market data."""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.event_bus import EventBus, Topics
from app.database.models import (
    EventJournalModel,
    EventOutboxModel,
    RawMarketEventModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.adapters.binance import build_raw_kline_event
from app.schemas.replay import ReplayCheckpoint
from app.schemas.events import BusMessage


async def test_raw_market_payload_is_persisted_idempotently():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    event = build_raw_kline_event(
        {
            "e": "kline",
            "E": 1767268800123,
            "k": {"s": "BTCUSDT", "i": "15m", "x": True, "T": 1767268800000},
        }
    )
    assert event is not None

    await repository.save_raw_market_event(event)
    await repository.save_raw_market_event(event)

    async with database.session() as session:
        count = await session.scalar(select(func.count()).select_from(RawMarketEventModel))
        stored = await session.get(RawMarketEventModel, event.event_id)
    await database.dispose()

    assert count == 1
    assert stored is not None
    assert stored.payload == event.payload
    assert stored.payload_sha256 == event.payload_sha256


async def test_bus_message_is_journaled_once_before_delivery():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    received: list[str] = []

    async def handler(message):
        received.append(message.event_id)

    bus = EventBus(journal=repository.save_bus_message, max_processed_event_ids=1)
    bus.subscribe(Topics.SYSTEM_EVENTS, handler)

    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STARTED",
        {"mode": "PAPER"},
        source="test",
        correlation_id="correlation-journal",
        event_id="journal-event-id",
    )
    # Evict the first ID from the in-memory window.
    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STOPPED",
        {"mode": "PAPER"},
        source="test",
        correlation_id="correlation-other",
        event_id="other-event-id",
    )
    # Durable journal still prevents redelivery after in-memory eviction.
    await bus.publish(
        Topics.SYSTEM_EVENTS,
        "SYSTEM_STARTED",
        {"mode": "PAPER"},
        source="test",
        correlation_id="correlation-journal",
        event_id="journal-event-id",
    )

    async with database.session() as session:
        count = await session.scalar(select(func.count()).select_from(EventJournalModel))
        stored = await session.scalar(select(EventJournalModel))
    await database.dispose()

    assert count == 2
    assert stored is not None
    assert stored.schema_version == "1.0.0"
    assert stored.payload == {"mode": "PAPER"}
    assert received == ["journal-event-id", "other-event-id"]


async def test_bus_message_cohort_journal_and_outbox_are_idempotent():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    messages = [
        BusMessage(
            event_id=f"cohort-event-{index}",
            correlation_id=f"cohort-correlation-{index}",
            topic=Topics.SYSTEM_EVENTS,
            event_type="SYSTEM_STARTED",
            source="test",
            payload={"index": index},
        )
        for index in range(3)
    ]

    first = await repository.save_bus_messages(messages)
    repeated = await repository.save_bus_messages(messages)
    await repository.mark_bus_messages_published(
        [
            (message.event_id, f"broker-{index}")
            for index, message in enumerate(messages)
        ]
    )
    published = await repository.save_bus_messages(messages)
    await repository.mark_bus_messages_failed(
        [messages[0].event_id],
        "ConnectionError",
    )

    async with database.session() as session:
        journal_count = await session.scalar(
            select(func.count()).select_from(EventJournalModel)
        )
        outbox_rows = list(
            await session.scalars(
                select(EventOutboxModel).order_by(
                    EventOutboxModel.event_id
                )
            )
        )
    await database.dispose()

    assert all(result.inserted for result in first.values())
    assert all(
        not result.inserted and not result.broker_published
        for result in repeated.values()
    )
    assert all(
        not result.inserted and result.broker_published
        for result in published.values()
    )
    assert journal_count == 3
    assert len(outbox_rows) == 3
    assert outbox_rows[0].publish_attempts == 2
    assert outbox_rows[0].last_error_type == "ConnectionError"
    assert [
        row.broker_message_id for row in outbox_rows
    ] == ["broker-0", "broker-1", "broker-2"]


async def test_replay_checkpoint_upsert_is_atomic_and_loadable():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    initial = ReplayCheckpoint(
        replay_id="database-replay",
        consumer_name="market-replay",
        topic="market.replay.v1",
        dataset_hash="a" * 64,
    )
    await repository.save_replay_checkpoint(initial)
    await repository.save_replay_checkpoint(
        initial.model_copy(
            update={
                "next_offset": 12,
                "last_event_id": "b" * 64,
                "events_processed": 12,
                "status": "RUNNING",
            }
        )
    )

    loaded = await repository.load_replay_checkpoint(
        "database-replay", "market-replay", "market.replay.v1"
    )
    await database.dispose()

    assert loaded is not None
    assert loaded.next_offset == 12
    assert loaded.last_event_id == "b" * 64

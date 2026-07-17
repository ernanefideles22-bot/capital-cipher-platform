"""Persistence tests for replayable events and raw public market data."""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.event_bus import EventBus, Topics
from app.database.models import EventJournalModel, RawMarketEventModel
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.adapters.binance import build_raw_kline_event


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

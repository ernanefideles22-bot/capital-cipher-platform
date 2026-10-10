"""Durable raw market cohort tests."""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.event_bus import EventBus, Topics
from app.database.models import RawMarketEventModel
from app.database.session import Database
from app.market_data.adapters.binance import build_raw_kline_event
from app.market_data.raw_batch import RawMarketEventBatchPersister


def _payload(symbol: str, event_ms: int) -> dict:
    return {
        "e": "kline",
        "E": event_ms,
        "k": {
            "s": symbol,
            "i": "5m",
            "x": False,
            "o": "100",
            "h": "102",
            "l": "99",
            "c": "101",
            "v": "10",
            "T": event_ms,
        },
    }


async def test_raw_cohort_is_persisted_and_delivered_as_full_batch():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    delivered: list[str] = []

    async def capture(message):
        delivered.append(message.event_id)

    event_bus = EventBus()
    event_bus.subscribe(Topics.RAW_MARKET_EVENTS, capture)
    persister = RawMarketEventBatchPersister(database, event_bus)
    events = [
        build_raw_kline_event(_payload("BTCUSDT", 1767268800000)),
        build_raw_kline_event(_payload("BTCUSDT", 1767268801000)),
        build_raw_kline_event(_payload("ETHUSDT", 1767268802000)),
    ]
    assert all(event is not None for event in events)
    cohort = [event for event in events if event is not None]

    await persister.persist(cohort)

    async with database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(RawMarketEventModel))
    assert count == 3
    assert delivered == [event.event_id for event in cohort]

    # Exact replay is idempotent in both raw storage and EventBus delivery.
    await persister.persist(cohort)
    async with database.session_factory() as session:
        replay_count = await session.scalar(
            select(func.count()).select_from(RawMarketEventModel)
        )
    assert replay_count == 3
    assert delivered == [event.event_id for event in cohort]

    await database.dispose()

"""Durable batch persistence for public raw market events.

The Month 2 market-data contract requires every public exchange payload to be
stored and journaled before normalization or analysis. This helper preserves
that fail-closed boundary while amortizing hosted PostgreSQL round trips across
a bounded cohort.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.errors import DatabaseError
from app.core.event_bus import EventBus, EventPublication, Topics
from app.database.models import RawMarketEventModel
from app.database.session import Database
from app.schemas.events import EventTypes
from app.schemas.market import RawMarketEvent


class RawMarketEventBatchPersister:
    """Persist raw source payloads and journal/publish them as one cohort."""

    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._database = database
        self._event_bus = event_bus

    @staticmethod
    def _deduplicate(events: list[RawMarketEvent]) -> list[RawMarketEvent]:
        unique: dict[str, RawMarketEvent] = {}
        for event in events:
            existing = unique.get(event.event_id)
            if existing is not None and existing != event:
                raise ValueError(
                    "Conflicting raw market payloads share one event identity"
                )
            unique.setdefault(event.event_id, event)
        return list(unique.values())

    async def persist(self, events: list[RawMarketEvent]) -> None:
        events = self._deduplicate(events)
        if not events:
            return

        values = [
            {
                "event_id": event.event_id,
                "schema_version": event.schema_version,
                "source": event.source,
                "exchange": event.exchange.value,
                "event_type": event.event_type,
                "symbol": event.symbol,
                "occurred_at": event.occurred_at,
                "received_at": event.received_at,
                "payload": event.payload,
                "payload_sha256": event.payload_sha256,
            }
            for event in events
        ]

        try:
            async with self._database.session() as session, session.begin():
                dialect = self._database.engine.dialect.name
                if dialect == "postgresql":
                    statement = postgresql_insert(RawMarketEventModel)
                elif dialect == "sqlite":
                    statement = sqlite_insert(RawMarketEventModel)
                else:
                    statement = None

                if statement is not None:
                    await session.execute(
                        statement.values(values).on_conflict_do_nothing(
                            index_elements=["event_id"]
                        )
                    )
                else:
                    existing_ids = set(
                        await session.scalars(
                            select(RawMarketEventModel.event_id).where(
                                RawMarketEventModel.event_id.in_(
                                    [event.event_id for event in events]
                                )
                            )
                        )
                    )
                    session.add_all(
                        RawMarketEventModel(**value)
                        for value in values
                        if value["event_id"] not in existing_ids
                    )
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist raw market event cohort: {exc}"
            ) from exc

        # Journal and broker publication are already batch-capable. Await the
        # whole cohort so a broker/journal failure suppresses normalization of
        # any closed candle contained in this raw cohort.
        await self._event_bus.publish_many(
            [
                EventPublication(
                    topic=Topics.RAW_MARKET_EVENTS,
                    event_type=EventTypes.RAW_MARKET_EVENT_RECEIVED,
                    payload=event.model_dump(mode="json"),
                    source=event.source,
                    correlation_id=event.event_id,
                    event_id=event.event_id,
                )
                for event in events
            ]
        )

"""Hot-path durable batching for public raw market events.

This module is intentionally small: it preserves the Month 3 invariant that
public provider payloads are durable before normalization while avoiding one
PostgreSQL transaction per WebSocket frame.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.errors import DatabaseError
from app.database.models import RawMarketEventModel
from app.database.session import Database
from app.schemas.market import RawMarketEvent


class RawMarketBatchStore:
    """Append immutable raw provider events idempotently in one transaction."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def _insert(self):
        dialect = self._database.engine.dialect.name
        if dialect == "postgresql":
            return postgresql_insert(RawMarketEventModel)
        if dialect == "sqlite":
            return sqlite_insert(RawMarketEventModel)
        return None

    @staticmethod
    def _values(event: RawMarketEvent) -> dict:
        return {
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

    async def save_many(self, events: list[RawMarketEvent]) -> int:
        """Persist a cohort once; exact duplicate identities are ignored."""

        if not events:
            return 0
        unique: dict[str, RawMarketEvent] = {}
        for event in events:
            existing = unique.get(event.event_id)
            if existing is not None and existing != event:
                raise ValueError("Raw market event identity conflict in batch")
            unique[event.event_id] = event
        values = [self._values(event) for event in unique.values()]
        try:
            async with self._database.session() as session, session.begin():
                statement = self._insert()
                if statement is not None:
                    inserted = await session.scalars(
                        statement.values(values)
                        .on_conflict_do_nothing(index_elements=["event_id"])
                        .returning(RawMarketEventModel.event_id)
                    )
                    return len(list(inserted))

                existing_ids = set(
                    await session.scalars(
                        select(RawMarketEventModel.event_id).where(
                            RawMarketEventModel.event_id.in_(unique)
                        )
                    )
                )
                missing = [
                    event
                    for event_id, event in unique.items()
                    if event_id not in existing_ids
                ]
                session.add_all(
                    [RawMarketEventModel(**self._values(event)) for event in missing]
                )
                return len(missing)
        except Exception as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(
                f"Failed to persist raw market event cohort: {exc}"
            ) from exc

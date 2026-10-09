"""Batch query bridge for durable outbox publication state.

This helper keeps the core OutboxDispatcher broker-neutral while allowing the
current Repository implementation to recheck a cohort with one database
round-trip. It can be removed once Repository exposes the same method directly.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import select

from app.core.errors import DatabaseError
from app.database.models import EventOutboxModel


async def list_published_bus_message_ids(
    repository: Any,
    event_ids: Collection[str],
) -> set[str] | None:
    """Return published ids in one query when a durable DB is available.

    ``None`` means the supplied repository does not expose the application's
    durable database handle, so callers may use their compatibility fallback.
    """

    unique_ids = list(dict.fromkeys(event_ids))
    if not unique_ids:
        return set()
    database = getattr(repository, "_db", None)
    if database is None:
        return None
    try:
        async with database.session() as session:
            rows = await session.scalars(
                select(EventOutboxModel.event_id).where(
                    EventOutboxModel.event_id.in_(unique_ids),
                    EventOutboxModel.published_at.is_not(None),
                )
            )
            return set(rows)
    except Exception as exc:
        raise DatabaseError(
            f"Failed to inspect bus message publication cohort: {exc}"
        ) from exc

"""Indexed batch query bridges for durable outbox recovery.

The dispatcher remains broker-neutral while PostgreSQL-backed repositories use
query shapes that follow the outbox indexes. In particular, normal polling must
never sort the wide event journal merely to discover that no messages are
pending.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import func, select

from app.core.errors import DatabaseError
from app.database.models import EventJournalModel, EventOutboxModel
from app.schemas.events import BusMessage


def _bus_message(row: EventJournalModel) -> BusMessage:
    return BusMessage(
        message_id=row.message_id,
        event_id=row.event_id,
        correlation_id=row.correlation_id,
        topic=row.topic,
        event_type=row.event_type,
        source=row.source,
        timestamp=row.created_at,
        schema_version=row.schema_version,
        payload=row.payload,
    )


def _pending_bus_messages_statement(limit: int):
    """Build the hot-path pending query from the indexed outbox side.

    ``ix_event_outbox_pending_created`` is a partial index on ``created_at``
    where ``published_at IS NULL``. Starting from that relation means an empty
    outbox is an indexed empty lookup instead of a 900k-row journal sort.
    """

    return (
        select(EventJournalModel)
        .select_from(EventOutboxModel)
        .join(
            EventJournalModel,
            EventJournalModel.event_id == EventOutboxModel.event_id,
        )
        .where(EventOutboxModel.published_at.is_(None))
        .order_by(EventOutboxModel.created_at)
        .limit(limit)
    )


def _untracked_bus_messages_statement(limit: int):
    """Find crash-gap journal rows that never acquired an outbox row."""

    return (
        select(EventJournalModel)
        .outerjoin(
            EventOutboxModel,
            EventOutboxModel.event_id == EventJournalModel.event_id,
        )
        .where(EventOutboxModel.event_id.is_(None))
        .order_by(EventJournalModel.created_at)
        .limit(limit)
    )


async def list_pending_bus_messages(
    repository: Any,
    limit: int,
) -> list[BusMessage] | None:
    """Read normal pending messages through the partial outbox index.

    ``None`` means the supplied repository has no application DB handle, so the
    caller should use its compatibility repository method.
    """

    database = getattr(repository, "_db", None)
    if database is None:
        return None
    try:
        async with database.session() as session:
            rows = await session.scalars(_pending_bus_messages_statement(limit))
            return [_bus_message(row) for row in rows]
    except Exception as exc:
        raise DatabaseError(
            f"Failed to load indexed pending bus message cohort: {exc}"
        ) from exc


async def list_untracked_bus_messages(
    repository: Any,
    limit: int,
) -> list[BusMessage] | None:
    """Recover journal/outbox crash gaps without penalizing every poll.

    The outbox event id is a foreign key into the journal, therefore equal table
    cardinalities prove there are no journal rows missing from the outbox. Only
    when the counts differ do we execute the anti-join. The dispatcher invokes
    this bridge periodically rather than on the one-second hot path.
    """

    database = getattr(repository, "_db", None)
    if database is None:
        return None
    try:
        async with database.session() as session:
            journal_count = int(
                await session.scalar(
                    select(func.count()).select_from(EventJournalModel)
                )
                or 0
            )
            outbox_count = int(
                await session.scalar(
                    select(func.count()).select_from(EventOutboxModel)
                )
                or 0
            )
            if journal_count == outbox_count:
                return []
            if outbox_count > journal_count:
                raise DatabaseError(
                    "Outbox cardinality exceeds journal cardinality"
                )
            rows = await session.scalars(
                _untracked_bus_messages_statement(limit)
            )
            return [_bus_message(row) for row in rows]
    except DatabaseError:
        raise
    except Exception as exc:
        raise DatabaseError(
            f"Failed to recover untracked bus message cohort: {exc}"
        ) from exc


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

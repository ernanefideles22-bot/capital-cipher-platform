"""Bounded, per-event coordination for broker publication."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class _EventLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PublicationCoordinator:
    """Serialize one event ID while allowing unrelated events to overlap."""

    def __init__(self, *, max_concurrency: int = 16) -> None:
        if max_concurrency < 1 or max_concurrency > 1_000:
            raise ValueError("max_concurrency must be between 1 and 1000")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._guard = asyncio.Lock()
        self._event_locks: dict[str, _EventLock] = {}

    @asynccontextmanager
    async def hold(self, event_id: str) -> AsyncIterator[None]:
        async with self.hold_many([event_id]):
            yield

    @asynccontextmanager
    async def hold_many(
        self,
        event_ids: list[str],
    ) -> AsyncIterator[None]:
        unique_event_ids = sorted(set(event_ids))
        if not unique_event_ids or any(not event_id for event_id in unique_event_ids):
            raise ValueError("at least one non-empty event_id is required")

        entries: list[_EventLock] = []
        async with self._guard:
            for event_id in unique_event_ids:
                entry = self._event_locks.setdefault(event_id, _EventLock())
                entry.users += 1
                entries.append(entry)

        acquired: list[_EventLock] = []
        try:
            for entry in entries:
                await entry.lock.acquire()
                acquired.append(entry)
            async with self._semaphore:
                yield
        finally:
            for entry in reversed(acquired):
                entry.lock.release()
            async with self._guard:
                for event_id, entry in zip(unique_event_ids, entries, strict=True):
                    entry.users -= 1
                    if entry.users == 0:
                        self._event_locks.pop(event_id, None)

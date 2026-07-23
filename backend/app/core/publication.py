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
        if not event_id:
            raise ValueError("event_id is required")

        async with self._guard:
            entry = self._event_locks.setdefault(event_id, _EventLock())
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            async with self._semaphore:
                yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._event_locks.pop(event_id, None)

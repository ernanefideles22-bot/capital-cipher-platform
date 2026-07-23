"""Broker-neutral transport boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.schemas.events import BusMessage


@dataclass(frozen=True)
class BrokerRecord:
    stream_id: str
    message: BusMessage


class EventTransport(Protocol):
    async def healthcheck(self) -> bool: ...

    async def publish(self, message: BusMessage) -> str: ...

    async def publish_many(self, messages: list[BusMessage]) -> list[str]: ...

    async def read_after(
        self,
        topic: str,
        *,
        after_id: str = "0-0",
        count: int = 100,
        block_ms: int = 0,
    ) -> list[BrokerRecord]: ...

    async def close(self) -> None: ...

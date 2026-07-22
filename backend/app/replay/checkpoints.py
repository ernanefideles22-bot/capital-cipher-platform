"""Replay checkpoint storage boundary and in-memory implementation."""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.schemas.replay import ReplayCheckpoint


class CheckpointStore(Protocol):
    async def load_replay_checkpoint(
        self,
        replay_id: str,
        consumer_name: str,
        topic: str,
    ) -> ReplayCheckpoint | None: ...

    async def save_replay_checkpoint(self, checkpoint: ReplayCheckpoint) -> None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[tuple[str, str, str], ReplayCheckpoint] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(
        replay_id: str, consumer_name: str, topic: str
    ) -> tuple[str, str, str]:
        return (replay_id, consumer_name, topic)

    async def load_replay_checkpoint(
        self,
        replay_id: str,
        consumer_name: str,
        topic: str,
    ) -> ReplayCheckpoint | None:
        async with self._lock:
            checkpoint = self._checkpoints.get(
                self._key(replay_id, consumer_name, topic)
            )
            return checkpoint.model_copy(deep=True) if checkpoint is not None else None

    async def save_replay_checkpoint(self, checkpoint: ReplayCheckpoint) -> None:
        async with self._lock:
            self._checkpoints[
                self._key(
                    checkpoint.replay_id,
                    checkpoint.consumer_name,
                    checkpoint.topic,
                )
            ] = checkpoint.model_copy(deep=True)

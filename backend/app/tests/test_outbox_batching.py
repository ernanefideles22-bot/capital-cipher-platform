"""Regression tests for bounded, batched durable outbox replay."""

from __future__ import annotations

import pytest

import app.core.outbox as outbox_module
from app.core.outbox import OutboxDispatcher
from app.schemas.events import BusMessage


def _message(event_id: str) -> BusMessage:
    return BusMessage(
        event_id=event_id,
        correlation_id="corr-1",
        topic="agent.outputs.v1",
        event_type="AGENT_COMPLETED",
        source="test",
        payload={"event_id": event_id},
    )


class _Repository:
    def __init__(self, pending: list[BusMessage]) -> None:
        self.pending = pending
        self.legacy_checks = 0
        self.marked_published: list[list[tuple[str, str]]] = []
        self.marked_failed: list[tuple[list[str], str]] = []

    async def list_pending_bus_messages(self, limit: int = 100):
        return self.pending[:limit]

    async def is_bus_message_published(self, event_id: str) -> bool:
        self.legacy_checks += 1
        raise AssertionError("legacy per-event publication lookup must not run")

    async def mark_bus_message_published(self, event_id: str, broker_id: str):
        raise AssertionError("legacy per-event mark must not run")

    async def mark_bus_messages_published(self, published):
        self.marked_published.append(list(published))

    async def mark_bus_message_failed(self, event_id: str, error_type: str):
        raise AssertionError("legacy per-event failure mark must not run")

    async def mark_bus_messages_failed(self, event_ids, error_type: str):
        self.marked_failed.append((list(event_ids), error_type))


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[str]] = []

    async def publish_many(self, messages: list[BusMessage]) -> list[str]:
        ids = [message.event_id for message in messages]
        self.batches.append(ids)
        if self.fail:
            raise RuntimeError("broker unavailable")
        return [f"redis-{event_id}" for event_id in ids]

    async def publish(self, message: BusMessage) -> str:
        raise AssertionError("legacy single publish must not run")


@pytest.mark.asyncio
async def test_outbox_rechecks_and_publishes_cohort_in_batches(monkeypatch):
    repository = _Repository([_message("e1"), _message("e2"), _message("e3")])
    transport = _Transport()
    bridge_calls: list[list[str]] = []

    async def _published_ids(_repository, event_ids):
        bridge_calls.append(list(event_ids))
        return {"e2"}

    monkeypatch.setattr(
        outbox_module,
        "durable_published_bus_message_ids",
        _published_ids,
    )

    result = await OutboxDispatcher(repository, transport).drain_once()

    assert bridge_calls == [["e1", "e2", "e3"]]
    assert repository.legacy_checks == 0
    assert transport.batches == [["e1", "e3"]]
    assert repository.marked_published == [
        [("e1", "redis-e1"), ("e3", "redis-e3")]
    ]
    assert repository.marked_failed == []
    assert result.attempted == 3
    assert result.published == 2
    assert result.failed == 0


@pytest.mark.asyncio
async def test_outbox_marks_broker_failure_for_replay_cohort(monkeypatch):
    repository = _Repository([_message("e1"), _message("e2"), _message("e3")])
    transport = _Transport(fail=True)

    async def _published_ids(_repository, event_ids):
        assert list(event_ids) == ["e1", "e2", "e3"]
        return {"e2"}

    monkeypatch.setattr(
        outbox_module,
        "durable_published_bus_message_ids",
        _published_ids,
    )

    result = await OutboxDispatcher(repository, transport).drain_once()

    assert repository.legacy_checks == 0
    assert transport.batches == [["e1", "e3"]]
    assert repository.marked_published == []
    assert repository.marked_failed == [(["e1", "e3"], "RuntimeError")]
    assert result.attempted == 3
    assert result.published == 0
    assert result.failed == 2

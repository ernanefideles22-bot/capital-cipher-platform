"""Deterministic replay resume and dataset-integrity tests."""

from __future__ import annotations

import pytest

from app.market_data.adapters.replay import (
    REPLAY_TOPIC,
    ReplayDatasetChangedError,
    ReplayMarketDataAdapter,
)
from app.replay.checkpoints import InMemoryCheckpointStore
from app.tests.conftest import make_series


async def test_replay_resumes_after_last_confirmed_event():
    candles = make_series([100.0, 101.0, 102.0])
    store = InMemoryCheckpointStore()
    first_seen: list[float] = []

    async def fail_on_second(candle):
        first_seen.append(candle.close)
        if len(first_seen) == 2:
            raise RuntimeError("simulated consumer crash")

    first = ReplayMarketDataAdapter(
        candles,
        replay_id="resume-test",
        checkpoint_store=store,
    )
    first.on_candle = fail_on_second
    with pytest.raises(RuntimeError):
        await first.replay()

    checkpoint = await store.load_replay_checkpoint(
        "resume-test", "market-replay", REPLAY_TOPIC
    )
    assert checkpoint is not None
    assert checkpoint.next_offset == 1
    assert checkpoint.events_processed == 1
    assert checkpoint.status == "FAILED"

    resumed_seen: list[float] = []

    async def collect(candle):
        resumed_seen.append(candle.close)

    resumed = ReplayMarketDataAdapter(
        candles,
        replay_id="resume-test",
        checkpoint_store=store,
    )
    resumed.on_candle = collect
    result = await resumed.replay()

    assert resumed_seen == [101.0, 102.0]
    assert result.start_offset == 1
    assert result.next_offset == 3
    assert result.events_emitted == 2
    assert result.completed is True


async def test_completed_replay_is_not_emitted_twice():
    candles = make_series([100.0, 101.0])
    store = InMemoryCheckpointStore()
    adapter = ReplayMarketDataAdapter(
        candles,
        replay_id="completed-test",
        checkpoint_store=store,
    )
    adapter.on_candle = lambda candle: _async_noop()
    first = await adapter.replay()
    second = await adapter.replay()

    assert first.events_emitted == 2
    assert second.events_emitted == 0
    assert second.resumed is True
    assert second.completed is True


async def test_empty_replay_is_immediately_completed():
    store = InMemoryCheckpointStore()
    adapter = ReplayMarketDataAdapter(
        [],
        replay_id="empty-test",
        checkpoint_store=store,
    )

    result = await adapter.replay()
    checkpoint = await store.load_replay_checkpoint(
        "empty-test", "market-replay", REPLAY_TOPIC
    )

    assert result.events_emitted == 0
    assert result.next_offset == 0
    assert result.completed is True
    assert checkpoint is not None
    assert checkpoint.status == "COMPLETED"
    assert checkpoint.completed_at is not None


async def _async_noop():
    return None


async def test_changed_dataset_cannot_reuse_existing_checkpoint():
    store = InMemoryCheckpointStore()
    original = ReplayMarketDataAdapter(
        make_series([100.0, 101.0]),
        replay_id="stable-replay-id",
        checkpoint_store=store,
    )
    original.on_candle = lambda candle: _async_noop()
    await original.replay()

    changed = ReplayMarketDataAdapter(
        make_series([100.0, 999.0]),
        replay_id="stable-replay-id",
        checkpoint_store=store,
    )
    changed.on_candle = lambda candle: _async_noop()
    with pytest.raises(ReplayDatasetChangedError):
        await changed.replay()

"""Live market continuity tests: hydrate/repair history, never trade retroactively."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.market_data.continuity import LiveCandleContinuityProcessor
from app.market_data.store import CandleStore
from app.schemas.common import Exchange
from app.schemas.market import Candle


BASE = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)


def _candle(closed_at: datetime, close: float = 100.0) -> Candle:
    return Candle(
        exchange=Exchange.BINANCE,
        symbol="BTCUSDT",
        timeframe="5m",
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10,
        closed_at=closed_at,
    )


class FakeRepository:
    def __init__(self, recovered: list[Candle]) -> None:
        self.rows = list(recovered)
        self.calls: list[dict] = []

    async def list_candles(self, **kwargs) -> list[Candle]:
        self.calls.append(kwargs)
        start_at = kwargs.get("start_at")
        end_at = kwargs.get("end_at")
        limit = kwargs.get("limit", len(self.rows))
        eligible = [
            candle
            for candle in self.rows
            if (start_at is None or candle.closed_at >= start_at)
            and (end_at is None or candle.closed_at <= end_at)
        ]
        return sorted(eligible, key=lambda item: item.closed_at)[:limit]


class FakeBackfill:
    def __init__(self, *, status: str = "COMPLETED", on_run=None) -> None:
        self.status = status
        self.requests = []
        self.on_run = on_run

    async def run(self, request):
        self.requests.append(request)
        if self.on_run is not None:
            self.on_run(request)
        return SimpleNamespace(status=self.status, job_id="bf-1")


class RecordingOrchestrator:
    def __init__(self, store: CandleStore) -> None:
        self.store = store
        self.seen: list[Candle] = []

    async def on_candle_closed(self, candle: Candle):
        self.seen.append(candle)
        self.store.add(candle)
        return candle


async def test_empty_store_hydrates_persisted_history_before_current_decision():
    store = CandleStore()
    history = [
        _candle(BASE - timedelta(minutes=15), 97),
        _candle(BASE - timedelta(minutes=10), 98),
        _candle(BASE - timedelta(minutes=5), 99),
    ]
    repository = FakeRepository(history)
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
        history_limit=3,
        specialist_history_candles=3,
    )

    current = _candle(BASE, 100)
    result = await processor.handle(current)

    assert result == current
    assert orchestrator.seen == [current]
    assert backfill.requests == []
    assert len(repository.calls) == 1
    hydration_call = repository.calls[0]
    assert hydration_call["end_at"] == BASE - timedelta(minutes=5)
    assert hydration_call["start_at"] == BASE - timedelta(minutes=15)
    assert hydration_call["limit"] == 3
    # Persisted candles only hydrate indicator state; none is evaluated as a
    # current event, so no retroactive decision can be created.
    assert [item.closed_at for item in store.get("BINANCE", "BTCUSDT", "5m")] == [
        *(item.closed_at for item in history),
        current.closed_at,
    ]


async def test_initial_hydration_keeps_only_newest_contiguous_suffix():
    store = CandleStore()
    persisted = [
        _candle(BASE - timedelta(minutes=25), 95),
        _candle(BASE - timedelta(minutes=20), 96),
        # Deliberate historical hole at BASE-15m. The older prefix must not be
        # joined to the latest contiguous suffix used by rolling indicators.
        _candle(BASE - timedelta(minutes=10), 98),
        _candle(BASE - timedelta(minutes=5), 99),
    ]
    repository = FakeRepository(persisted)
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
        history_limit=4,
        specialist_history_candles=2,
    )

    current = _candle(BASE, 100)
    await processor.handle(current)

    assert backfill.requests == []
    assert orchestrator.seen == [current]
    assert [item.closed_at for item in store.get("BINANCE", "BTCUSDT", "5m")] == [
        BASE - timedelta(minutes=10),
        BASE - timedelta(minutes=5),
        BASE,
    ]


async def test_short_history_is_repaired_before_specialists_run():
    store = CandleStore()
    repository = FakeRepository(
        [_candle(BASE - timedelta(minutes=5), 99)]
    )
    repaired_history = [
        _candle(BASE - timedelta(minutes=20), 96),
        _candle(BASE - timedelta(minutes=15), 97),
        _candle(BASE - timedelta(minutes=10), 98),
        _candle(BASE - timedelta(minutes=5), 99),
    ]

    def repair_repository(_request) -> None:
        repository.rows = list(repaired_history)

    backfill = FakeBackfill(on_run=repair_repository)
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
        history_limit=6,
        specialist_history_candles=4,
    )

    current = _candle(BASE, 100)
    result = await processor.handle(current)

    assert result == current
    assert len(backfill.requests) == 1
    request = backfill.requests[0]
    assert request.start_at == BASE - timedelta(minutes=20)
    assert request.end_at == BASE - timedelta(minutes=5)
    assert request.max_candles == 4
    # The repaired history is state only. Only the current live candle is
    # evaluated by the orchestrator.
    assert orchestrator.seen == [current]
    assert [item.closed_at for item in store.get("BINANCE", "BTCUSDT", "5m")] == [
        *(item.closed_at for item in repaired_history),
        current.closed_at,
    ]


async def test_contiguous_live_candle_does_not_backfill():
    store = CandleStore()
    store.add(_candle(BASE))
    repository = FakeRepository([])
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
    )

    current = _candle(BASE + timedelta(minutes=5), 101)
    result = await processor.handle(current)

    assert result == current
    assert orchestrator.seen == [current]
    assert backfill.requests == []
    assert repository.calls == []


async def test_gap_is_repaired_into_store_without_retroactive_decision():
    store = CandleStore()
    previous = _candle(BASE)
    recovered = _candle(BASE + timedelta(minutes=5), 101)
    current = _candle(BASE + timedelta(minutes=10), 102)
    store.add(previous)
    repository = FakeRepository([recovered])
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
    )

    await processor.handle(current)

    assert len(backfill.requests) == 1
    request = backfill.requests[0]
    assert request.start_at == recovered.closed_at
    assert request.end_at == recovered.closed_at
    assert request.max_candles == 1
    # Only the current live candle reaches decision-making. The recovered
    # historical candle hydrates state but never creates a retroactive cycle.
    assert orchestrator.seen == [current]
    assert [item.closed_at for item in store.get("BINANCE", "BTCUSDT", "5m")] == [
        previous.closed_at,
        recovered.closed_at,
        current.closed_at,
    ]


async def test_incomplete_recovery_blocks_current_live_decision():
    store = CandleStore()
    previous = _candle(BASE)
    current = _candle(BASE + timedelta(minutes=15), 103)
    store.add(previous)
    # Two candles are missing, but only one is returned after the backfill.
    repository = FakeRepository([_candle(BASE + timedelta(minutes=5), 101)])
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
    )

    result = await processor.handle(current)

    assert result is None
    assert orchestrator.seen == []
    assert [item.closed_at for item in store.get("BINANCE", "BTCUSDT", "5m")] == [
        previous.closed_at
    ]


async def test_non_completed_backfill_blocks_current_live_decision():
    store = CandleStore()
    previous = _candle(BASE)
    current = _candle(BASE + timedelta(minutes=10), 102)
    store.add(previous)
    repository = FakeRepository([])
    backfill = FakeBackfill(status="PARTIAL")
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
    )

    result = await processor.handle(current)

    assert result is None
    assert orchestrator.seen == []
    assert repository.calls == []


async def test_irregular_gap_is_not_guessed():
    store = CandleStore()
    previous = _candle(BASE)
    # 11 minutes is not aligned to the configured 5-minute cadence.
    current = _candle(BASE + timedelta(minutes=11), 102)
    store.add(previous)
    repository = FakeRepository([])
    backfill = FakeBackfill()
    orchestrator = RecordingOrchestrator(store)
    processor = LiveCandleContinuityProcessor(
        store=store,
        repository=repository,
        backfill_service=backfill,
        orchestrator=orchestrator,
    )

    result = await processor.handle(current)

    assert result is None
    assert backfill.requests == []
    assert orchestrator.seen == []

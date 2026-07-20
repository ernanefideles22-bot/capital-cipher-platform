"""Clock gates, public REST normalization, gap tracking, and backfill tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.trend import TrendAgent
from app.database.models import CandleObservationModel
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import DataQualityError
from app.market_data.adapters.binance_rest import BinancePublicRestClient
from app.market_data.adapters.bybit_rest import BybitPublicRestClient
from app.market_data.backfill import HistoricalBackfillService
from app.market_data.catalog import DataCatalog
from app.market_data.clock import (
    ExchangeClockMonitor,
    ExchangeClockRegistry,
    evaluate_clock_probe,
)
from app.market_data.gaps import GapService, detect_candle_gaps
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.service import Orchestrator
from app.schemas.backfill import HistoricalBackfillRequest
from app.schemas.common import Exchange
from app.tests.conftest import make_candle


def _closed_at(index: int = 0) -> datetime:
    return datetime(2026, 7, 1, 0, 14, 59, 999000, tzinfo=timezone.utc) + (
        timedelta(minutes=15) * index
    )


def test_clock_registry_fails_closed_for_unknown_stale_and_unsafe():
    registry = ExchangeClockRegistry(max_age_seconds=90)
    now = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
    assert registry.verdict(Exchange.BINANCE, now=now).trusted is False

    synced = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=now - timedelta(milliseconds=100),
        source_at=now - timedelta(milliseconds=50),
        response_received_at=now,
    )
    registry.record(Exchange.BINANCE, synced)
    assert registry.verdict(Exchange.BINANCE, now=now).trusted is True
    assert (
        registry.verdict(
            Exchange.BINANCE,
            now=now + timedelta(minutes=2),
        ).status
        == "UNKNOWN"
    )

    unsafe = evaluate_clock_probe(
        source="binance.server-time",
        request_started_at=now,
        source_at=now + timedelta(seconds=4),
        response_received_at=now + timedelta(milliseconds=100),
    )
    registry.record(Exchange.BINANCE, unsafe)
    assert (
        registry.verdict(
            Exchange.BINANCE,
            now=now + timedelta(milliseconds=100),
        ).status
        == "UNSAFE"
    )


async def test_binance_public_rest_probes_clock_and_paginates_klines():
    start = _closed_at(0)
    second = _closed_at(1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/time":
            return httpx.Response(
                200,
                json={"serverTime": int(datetime.now(timezone.utc).timestamp() * 1000)},
            )
        assert request.url.path == "/api/v3/klines"
        return httpx.Response(
            200,
            json=[
                [
                    int((start - timedelta(minutes=15) + timedelta(milliseconds=1)).timestamp() * 1000),
                    "100",
                    "102",
                    "99",
                    "101",
                    "10",
                    int(start.timestamp() * 1000),
                ],
                [
                    int((second - timedelta(minutes=15) + timedelta(milliseconds=1)).timestamp() * 1000),
                    "101",
                    "103",
                    "100",
                    "102",
                    "11",
                    int(second.timestamp() * 1000),
                ],
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://binance.test",
    ) as http_client:
        client = BinancePublicRestClient(client=http_client)
        observation = await client.probe_clock()
        candles = await client.fetch_candles(
            symbol="BTCUSDT",
            timeframe="15m",
            start_at=start,
            end_at=second,
            limit=10,
        )

    assert observation.status == "SYNCED"
    assert [candle.closed_at for candle in candles] == [start, second]
    assert candles[-1].close == 102


async def test_bybit_public_rest_normalizes_reverse_order_klines():
    start = _closed_at(0)
    second = _closed_at(1)

    def row(closed_at: datetime, close: str) -> list[str]:
        open_at = closed_at - timedelta(minutes=15) + timedelta(milliseconds=1)
        return [
            str(int(open_at.timestamp() * 1000)),
            "100",
            "103",
            "99",
            close,
            "10",
            "1000",
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v5/market/time":
            now_nanos = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"timeNano": str(now_nanos)},
                },
            )
        assert request.url.path == "/v5/market/kline"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": [row(second, "102"), row(start, "101")]},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://bybit.test",
    ) as http_client:
        client = BybitPublicRestClient(client=http_client)
        observation = await client.probe_clock()
        candles = await client.fetch_candles(
            symbol="BTCUSDT",
            timeframe="15m",
            start_at=start,
            end_at=second,
            limit=10,
        )

    assert observation.status == "SYNCED"
    assert [candle.closed_at for candle in candles] == [start, second]
    assert candles[-1].exchange == Exchange.BYBIT


def test_gap_detector_finds_leading_internal_and_trailing_ranges():
    candles = [
        make_candle(closed_at=_closed_at(1)),
        make_candle(closed_at=_closed_at(3)),
    ]
    gaps = detect_candle_gaps(
        candles,
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=_closed_at(0),
        end_at=_closed_at(4),
    )
    assert [(gap.start_at, gap.end_at, gap.missing_count) for gap in gaps] == [
        (_closed_at(0), _closed_at(0), 1),
        (_closed_at(2), _closed_at(2), 1),
        (_closed_at(4), _closed_at(4), 1),
    ]
    assert len({gap.gap_id for gap in gaps}) == 3


async def test_gap_scan_rejects_a_limit_that_would_truncate_the_range():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    service = GapService(Repository(database))
    with pytest.raises(DataQualityError, match="limit"):
        await service.scan(
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            start_at=_closed_at(0),
            end_at=_closed_at(2),
            limit=2,
        )
    await database.dispose()


class RecordingRepository:
    def __init__(self) -> None:
        self.system_events = []
        self.candle_writes = 0

    async def save_system_event(self, event):
        self.system_events.append(event)

    async def save_candle(self, candle, quality=None):
        self.candle_writes += 1
        return True


async def test_orchestrator_blocks_normalized_ingestion_on_unsafe_clock(
    paper_state_machine,
    event_bus,
    candle_store,
    risk_manager,
    paper_engine,
    audit_service,
):
    now = datetime.now(timezone.utc)
    registry = ExchangeClockRegistry(max_age_seconds=90)
    registry.record(
        Exchange.BINANCE,
        evaluate_clock_probe(
            source="binance.server-time",
            request_started_at=now - timedelta(milliseconds=10),
            source_at=now + timedelta(seconds=4),
            response_received_at=now,
        ),
    )
    repository = RecordingRepository()
    orchestrator = Orchestrator(
        state_machine=paper_state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        decision_engine=DecisionEngine(),
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        audit_service=audit_service,
        market_data_agent=MarketDataAgent(candle_store),
        quant_agent=QuantAgent(candle_store),
        trend_agent=TrendAgent(candle_store),
        repository=repository,
        clock_registry=registry,
        require_trusted_clock=True,
    )

    decision = await orchestrator.on_candle_closed(make_candle())

    assert decision is None
    assert repository.candle_writes == 0
    assert repository.system_events[0]["event_type"] == "CLOCK_GATE_BLOCKED"
    assert candle_store.get("BINANCE", "BTCUSDT", "15m") == []


async def test_stream_ingestion_automatically_records_detected_gap():
    context = build_context(
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            REQUIRE_TRUSTED_MARKET_CLOCK=False,
        ),
        with_database=True,
    )
    assert context.database is not None
    assert context.repository is not None
    await context.database.create_all()

    await context.orchestrator.on_candle_closed(
        make_candle(100, closed_at=_closed_at(0))
    )
    await context.orchestrator.on_candle_closed(
        make_candle(102, closed_at=_closed_at(2))
    )
    gaps = await context.repository.list_market_data_gaps(status="OPEN")

    if context.public_market_clients is not None:
        for client in context.public_market_clients.values():
            await client.aclose()
    await context.database.dispose()

    assert len(gaps) == 1
    assert gaps[0].start_at == _closed_at(1)


class FakePublicClient:
    exchange = Exchange.BINANCE
    source_name = "binance.test-public-rest"

    def __init__(self, candles, *, unsafe: bool = False) -> None:
        self.candles = candles
        self.unsafe = unsafe
        self.fetch_calls = 0

    async def probe_clock(self, **thresholds):
        now = datetime.now(timezone.utc)
        source_at = (
            now + timedelta(seconds=4)
            if self.unsafe
            else now - timedelta(milliseconds=5)
        )
        return evaluate_clock_probe(
            source="binance.server-time",
            request_started_at=now - timedelta(milliseconds=10),
            source_at=source_at,
            response_received_at=now,
            **thresholds,
        )

    async def fetch_candles(self, **request):
        self.fetch_calls += 1
        return list(self.candles)

    async def aclose(self):
        return None


async def _build_backfill_service(client: FakePublicClient):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    registry = ExchangeClockRegistry(max_age_seconds=90)
    monitor = ExchangeClockMonitor(
        {Exchange.BINANCE: client},
        registry,
        repository,
    )
    catalog = DataCatalog(repository)
    service = HistoricalBackfillService(
        repository=repository,
        clients={Exchange.BINANCE: client},
        clock_monitor=monitor,
        clock_registry=registry,
        gap_service=GapService(repository),
        data_catalog=catalog,
    )
    return database, repository, service


async def test_backfill_is_clock_gated_cataloged_and_idempotent():
    candles = [
        make_candle(100 + index, closed_at=_closed_at(index))
        for index in range(3)
    ]
    client = FakePublicClient(candles)
    database, repository, service = await _build_backfill_service(client)
    request = HistoricalBackfillRequest(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=_closed_at(0),
        end_at=_closed_at(2),
        max_candles=10,
    )

    first = await service.run(request)
    repeated = await service.run(request)
    loaded = await repository.load_historical_backfill_job(first.job_id)
    async with database.session() as session:
        count = await session.scalar(
            select(func.count()).select_from(CandleObservationModel)
        )
    await database.dispose()

    assert first.status == "COMPLETED"
    assert first.inserted_count == 3
    assert first.dataset_hash is not None
    assert repeated == first
    assert loaded == first
    assert client.fetch_calls == 1
    assert count == 3


async def test_unsafe_clock_blocks_historical_ingestion():
    client = FakePublicClient(
        [make_candle(closed_at=_closed_at(0))],
        unsafe=True,
    )
    database, repository, service = await _build_backfill_service(client)
    request = HistoricalBackfillRequest(
        start_at=_closed_at(0),
        end_at=_closed_at(0),
        max_candles=1,
    )

    job = await service.run(request)
    stored = await repository.list_candles(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
    )
    await database.dispose()

    assert job.status == "BLOCKED"
    assert job.clock_status == "UNSAFE"
    assert job.error_code == "CLOCK_UNTRUSTED"
    assert client.fetch_calls == 0
    assert stored == []


async def test_backfill_rejects_an_open_or_future_candle_range():
    future = datetime.now(timezone.utc) + timedelta(minutes=15)
    client = FakePublicClient([make_candle(closed_at=future)])
    database, _, service = await _build_backfill_service(client)
    job = await service.run(
        HistoricalBackfillRequest(
            start_at=future,
            end_at=future,
            max_candles=1,
        )
    )
    await database.dispose()

    assert job.status == "FAILED"
    assert job.error_code == "DATA_QUALITY_ERROR"
    assert client.fetch_calls == 0


async def test_partial_backfill_persists_remaining_gap_then_resolves_it():
    client = FakePublicClient(
        [
            make_candle(100, closed_at=_closed_at(0)),
            make_candle(102, closed_at=_closed_at(2)),
        ]
    )
    database, repository, service = await _build_backfill_service(client)
    request = HistoricalBackfillRequest(
        start_at=_closed_at(0),
        end_at=_closed_at(2),
        max_candles=3,
    )

    partial = await service.run(request)
    open_gaps = await repository.list_market_data_gaps(status="OPEN")
    await GapService(repository).scan(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=_closed_at(0),
        end_at=_closed_at(2),
        limit=3,
    )
    rescanned_gaps = await repository.list_market_data_gaps(status="OPEN")
    client.candles = [
        make_candle(100 + index, closed_at=_closed_at(index))
        for index in range(3)
    ]
    completed = await service.run(request)
    resolved = await repository.list_market_data_gaps(status="RESOLVED")
    await database.dispose()

    assert partial.status == "PARTIAL"
    assert len(open_gaps) == 1
    assert open_gaps[0].start_at == _closed_at(1)
    assert rescanned_gaps[0].backfill_job_id == partial.job_id
    assert completed.status == "COMPLETED"
    assert completed.attempt_count == 2
    assert len(resolved) == 1

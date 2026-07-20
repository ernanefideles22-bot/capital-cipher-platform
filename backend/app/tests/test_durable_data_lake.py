"""Durable queue, retry recovery, raw storage, and lineage tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient

from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.market_data.backfill import HistoricalBackfillService
from app.market_data.backfill_worker import HistoricalBackfillWorker
from app.market_data.catalog import DataCatalog
from app.market_data.clock import (
    ExchangeClockMonitor,
    ExchangeClockRegistry,
    evaluate_clock_probe,
)
from app.market_data.data_lake import (
    LocalContentAddressedBlobStore,
    RawDataLake,
)
from app.market_data.gaps import GapService
from app.schemas.backfill import HistoricalBackfillRequest
from app.schemas.common import Exchange
from app.schemas.data_lake import RawProviderPage
from app.tests.conftest import make_candle


def _closed_at(index: int = 0) -> datetime:
    return datetime(2026, 7, 1, 0, 14, 59, 999000, tzinfo=timezone.utc) + (
        timedelta(minutes=15) * index
    )


class ArchivingFakeClient:
    exchange = Exchange.BINANCE
    source_name = "binance.test-public-rest"

    def __init__(self, candles) -> None:
        self.candles = candles
        self.fetch_calls = 0

    async def probe_clock(self, **thresholds):
        received = datetime.now(timezone.utc)
        return evaluate_clock_probe(
            source="binance.server-time",
            request_started_at=received - timedelta(milliseconds=20),
            source_at=received - timedelta(milliseconds=10),
            response_received_at=received,
            **thresholds,
        )

    async def fetch_candles(self, **request):
        self.fetch_calls += 1
        on_page = request.get("on_page")
        if on_page is not None:
            await on_page(
                RawProviderPage(
                    source=self.source_name,
                    endpoint="/api/v3/klines",
                    request_params={
                        "symbol": request["symbol"],
                        "interval": request["timeframe"],
                    },
                    payload={
                        "rows": [
                            candle.model_dump(mode="json")
                            for candle in self.candles
                        ]
                    },
                    page_index=0,
                )
            )
        return list(self.candles)

    async def aclose(self):
        return None


async def _build_service(tmp_path, candles):
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    client = ArchivingFakeClient(candles)
    registry = ExchangeClockRegistry(max_age_seconds=90)
    monitor = ExchangeClockMonitor(
        {Exchange.BINANCE: client},
        registry,
        repository,
    )
    blob_store = LocalContentAddressedBlobStore(tmp_path / "lake")
    service = HistoricalBackfillService(
        repository=repository,
        clients={Exchange.BINANCE: client},
        clock_monitor=monitor,
        clock_registry=registry,
        gap_service=GapService(repository),
        data_catalog=DataCatalog(repository),
        raw_data_lake=RawDataLake(repository, blob_store),
    )
    return database, repository, client, blob_store, service


def _request(count: int = 1) -> HistoricalBackfillRequest:
    return HistoricalBackfillRequest(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=_closed_at(0),
        end_at=_closed_at(count - 1),
        max_candles=count,
    )


async def test_submit_claim_lease_recovery_and_dead_letter(tmp_path):
    database, repository, _, _, service = await _build_service(
        tmp_path,
        [make_candle(closed_at=_closed_at(0))],
    )
    submitted = await service.submit(_request(), max_attempts=2)
    repeated = await service.submit(_request(), max_attempts=2)
    first_claim_at = submitted.created_at + timedelta(seconds=1)

    first = await repository.claim_next_backfill(
        worker_id="worker-one",
        lease_seconds=10,
        now=first_claim_at,
    )
    unavailable = await repository.claim_next_backfill(
        worker_id="worker-two",
        lease_seconds=10,
        now=first_claim_at,
    )
    recovered = await repository.claim_next_backfill(
        worker_id="worker-two",
        lease_seconds=10,
        now=first_claim_at + timedelta(seconds=11),
    )
    exhausted = await repository.claim_next_backfill(
        worker_id="worker-three",
        lease_seconds=10,
        now=first_claim_at + timedelta(seconds=22),
    )
    final_queue = await repository.load_backfill_queue_item(submitted.job_id)
    final_job = await repository.load_historical_backfill_job(submitted.job_id)
    resubmitted = await service.submit(_request(), max_attempts=2)
    reset_queue = await repository.load_backfill_queue_item(submitted.job_id)
    await database.dispose()

    assert repeated == submitted
    assert first is not None
    assert first.attempt_count == 1
    assert unavailable is None
    assert recovered is not None
    assert recovered.attempt_count == 2
    assert recovered.leased_by == "worker-two"
    assert exhausted is None
    assert final_queue is not None
    assert final_queue.status == "DEAD_LETTER"
    assert final_queue.last_error_code == "BACKFILL_ATTEMPTS_EXHAUSTED"
    assert final_job is not None
    assert final_job.status == "FAILED"
    assert resubmitted.status == "PENDING"
    assert reset_queue is not None
    assert reset_queue.status == "PENDING"
    assert reset_queue.attempt_count == 0


async def test_worker_archives_raw_page_before_normalized_dataset(tmp_path):
    candles = [
        make_candle(100 + index, closed_at=_closed_at(index))
        for index in range(2)
    ]
    database, repository, client, blob_store, service = await _build_service(
        tmp_path,
        candles,
    )
    submitted = await service.submit(_request(2))
    worker = HistoricalBackfillWorker(
        repository=repository,
        service=service,
        worker_id="backfill-test-worker",
        poll_interval_seconds=0.01,
        lease_seconds=60,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    completed_queue = await worker.run_once()
    completed_job = await repository.load_historical_backfill_job(
        submitted.job_id
    )
    raw_pages = await repository.list_backfill_raw_pages(submitted.job_id)
    raw_object = await repository.load_raw_data_object(
        raw_pages[0].object_hash
    )
    stored_candles = await repository.list_candles(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
    )
    assert raw_object is not None
    raw_document = await blob_store.read_json(raw_object)
    duplicate_link = await RawDataLake(repository, blob_store).archive_page(
        job_id=submitted.job_id,
        attempt_count=1,
        page=RawProviderPage.model_validate(raw_document),
    )
    reloaded_raw_pages = await repository.list_backfill_raw_pages(
        submitted.job_id
    )
    stored_objects = list(blob_store.root.rglob("*.json.gz"))
    await database.dispose()

    assert completed_queue is not None
    assert completed_queue.status == "COMPLETED"
    assert completed_job is not None
    assert completed_job.status == "COMPLETED"
    assert completed_job.dataset_hash is not None
    assert client.fetch_calls == 1
    assert len(raw_pages) == 1
    assert duplicate_link == raw_pages[0]
    assert reloaded_raw_pages == raw_pages
    assert len(stored_objects) == 1
    assert raw_document["payload"]["rows"][0]["close"] == 100.0
    assert len(stored_candles) == 2


async def test_retryable_worker_result_is_rescheduled(tmp_path):
    database, repository, client, _, service = await _build_service(
        tmp_path,
        [
            make_candle(100, closed_at=_closed_at(0)),
            make_candle(102, closed_at=_closed_at(2)),
        ],
    )
    request = _request(3)
    submitted = await service.submit(request, max_attempts=3)
    worker = HistoricalBackfillWorker(
        repository=repository,
        service=service,
        worker_id="backfill-retry-worker",
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    retry = await worker.run_once()
    client.candles = [
        make_candle(100 + index, closed_at=_closed_at(index))
        for index in range(3)
    ]
    completed = await worker.run_once()
    job = await repository.load_historical_backfill_job(submitted.job_id)
    await database.dispose()

    assert retry is not None
    assert retry.status == "RETRY"
    assert retry.last_error_code == "BACKFILL_PARTIAL"
    assert completed is not None
    assert completed.status == "COMPLETED"
    assert job is not None
    assert job.status == "COMPLETED"
    assert job.attempt_count == 2


async def test_raw_archive_failure_blocks_normalized_persistence(tmp_path):
    database, repository, _, _, service = await _build_service(
        tmp_path,
        [make_candle(closed_at=_closed_at(0))],
    )

    class FailingLake:
        async def archive_page(self, **_):
            raise ValueError("simulated private object-store failure")

    service._raw_data_lake = FailingLake()
    result = await service.run(_request())
    stored = await repository.list_candles(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
    )
    await database.dispose()

    assert result.status == "FAILED"
    assert result.error_code == "VALIDATION_ERROR"
    assert stored == []


async def test_admin_api_enqueues_and_exposes_lineage(tmp_path):
    admin_key = "q" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        BACKFILL_WORKER_ENABLED=False,
        DATA_LAKE_ROOT=str(tmp_path / "api-lake"),
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    body = _request().model_dump(mode="json")

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/market/backfills",
                headers={"X-API-Key": admin_key},
                json=body,
            )
            job_id = created.json()["data"]["job"]["job_id"]
            loaded = await client.get(
                f"/api/v1/market/backfills/{job_id}",
                headers={"X-API-Key": admin_key},
            )
            lineage = await client.get(
                f"/api/v1/market/backfills/{job_id}/lineage",
                headers={"X-API-Key": admin_key},
            )

    assert created.status_code == 200
    assert created.json()["data"]["queue_item"]["status"] == "PENDING"
    assert loaded.status_code == 200
    assert loaded.json()["data"]["queue_item"]["queue_id"] == job_id
    assert lineage.status_code == 200
    assert lineage.json()["data"]["raw_pages"] == []

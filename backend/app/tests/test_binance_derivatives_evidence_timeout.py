from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agents.binance_derivatives_evidence import (
    BinanceUsdMDerivativesEvidenceCollector,
)
from app.agents.evaluation import SpecialistEvidenceService


class SlowEvidenceService(SpecialistEvidenceService):
    async def ingest(self, evidence):
        await asyncio.sleep(0.03)
        return await super().ingest(evidence)


@pytest.mark.asyncio
async def test_refresh_batch_survives_first_caller_timeout() -> None:
    as_of = datetime(2026, 10, 9, 10, 40, tzinfo=timezone.utc)
    end_time = int(as_of.timestamp() * 1_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/fundingRate":
            payload = [{
                "symbol": "BTCUSDT",
                "fundingTime": end_time - 3_600_000,
                "fundingRate": "0.0001",
            }]
        elif request.url.path == "/futures/data/openInterestHist":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": "100",
                    "sumOpenInterestValue": "100000",
                    "timestamp": end_time - 600_000,
                },
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": "101",
                    "sumOpenInterestValue": "101000",
                    "timestamp": end_time - 300_000,
                },
            ]
        elif request.url.path == "/futures/data/basis":
            payload = [{"basisRate": "-0.0005", "timestamp": end_time - 300_000}]
        elif request.url.path == "/futures/data/globalLongShortAccountRatio":
            payload = [{"longShortRatio": "1.2", "timestamp": end_time - 300_000}]
        elif request.url.path == "/futures/data/takerlongshortRatio":
            payload = [{"buySellRatio": "0.9", "timestamp": end_time - 600_000}]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json=payload)

    service = SlowEvidenceService()
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.test",
        transport=httpx.MockTransport(handler),
    )
    collector = BinanceUsdMDerivativesEvidenceCollector(
        service,
        client=client,
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                collector.refresh(
                    scope="BTCUSDT",
                    as_of=as_of,
                    timeframe="5m",
                ),
                timeout=0.01,
            )
        await asyncio.sleep(0.08)
        evidence = await service.list(
            domain="DERIVATIVES",
            scope="BTCUSDT",
            limit=20,
        )
    finally:
        await client.aclose()

    assert {item.metric_name for item in evidence} == {
        "funding_rate",
        "open_interest_change",
        "basis",
        "long_short_ratio",
        "taker_buy_sell_ratio",
    }

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app.agents.binance_derivatives_evidence import (
    BinanceUsdMDerivativesEvidenceCollector,
)
from app.agents.evaluation import SpecialistEvidenceService
from app.agents.month8_specialists import (
    DERIVATIVES_DEFINITIONS,
    ExternalEvidenceSpecialist,
)
from app.schemas.agents import AgentInput
from app.schemas.common import Signal


@pytest.mark.asyncio
async def test_binance_usdm_collector_ingests_five_as_of_metrics() -> None:
    as_of = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    end_time = int(as_of.timestamp() * 1_000)
    five_minutes = 5 * 60 * 1_000
    funding_time = end_time - 4 * 60 * 60 * 1_000
    seen_end_times: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        seen_end_times.append(int(query["endTime"][0]))
        if request.url.path == "/fapi/v1/fundingRate":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": funding_time,
                    "fundingRate": "0.0001",
                }
            ]
        elif request.url.path == "/futures/data/openInterestHist":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": "100.0",
                    "sumOpenInterestValue": "100000.0",
                    "timestamp": end_time - 2 * five_minutes,
                },
                {
                    "symbol": "BTCUSDT",
                    "sumOpenInterest": "110.0",
                    "sumOpenInterestValue": "111000.0",
                    "timestamp": end_time - five_minutes,
                },
            ]
        elif request.url.path == "/futures/data/basis":
            payload = [
                {
                    "pair": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "basisRate": "0.0012",
                    "timestamp": end_time - five_minutes,
                }
            ]
        elif request.url.path == "/futures/data/globalLongShortAccountRatio":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "longShortRatio": "1.25",
                    "longAccount": "0.5556",
                    "shortAccount": "0.4444",
                    "timestamp": end_time - five_minutes,
                }
            ]
        elif request.url.path == "/futures/data/takerlongshortRatio":
            payload = [
                {
                    "buySellRatio": "1.40",
                    "buyVol": "140.0",
                    "sellVol": "100.0",
                    "timestamp": end_time - five_minutes,
                }
            ]
        else:
            raise AssertionError(f"Unexpected path: {request.url.path}")
        return httpx.Response(200, json=payload)

    service = SpecialistEvidenceService()
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.test",
        transport=httpx.MockTransport(handler),
    )
    collector = BinanceUsdMDerivativesEvidenceCollector(
        service,
        client=client,
    )
    try:
        await collector.refresh(scope="BTCUSDT", as_of=as_of, timeframe="5m")
        evidence = await service.list(
            domain="DERIVATIVES",
            scope="BTCUSDT",
            limit=20,
        )
    finally:
        await client.aclose()

    by_metric = {item.metric_name: item for item in evidence}
    assert set(by_metric) == {
        "funding_rate",
        "open_interest_change",
        "basis",
        "long_short_ratio",
        "taker_buy_sell_ratio",
    }
    assert by_metric["funding_rate"].value == pytest.approx(0.0001)
    assert by_metric["open_interest_change"].value == pytest.approx(0.10)
    assert by_metric["basis"].value == pytest.approx(0.0012)
    assert by_metric["long_short_ratio"].value == pytest.approx(1.25)
    assert by_metric["taker_buy_sell_ratio"].value == pytest.approx(1.40)
    assert all(item.observed_at <= as_of for item in evidence)
    assert seen_end_times == [end_time] * 5


@pytest.mark.asyncio
async def test_supported_derivatives_specialist_refreshes_governed_evidence() -> None:
    as_of = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    end_time = int(as_of.timestamp() * 1_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/fundingRate":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": int(
                        (as_of - timedelta(hours=4)).timestamp() * 1_000
                    ),
                    "fundingRate": "0.0002",
                }
            ]
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
                    "sumOpenInterestValue": "101100",
                    "timestamp": end_time - 300_000,
                },
            ]
        elif request.url.path == "/futures/data/basis":
            payload = [{"basisRate": "0.001", "timestamp": end_time - 300_000}]
        elif request.url.path == "/futures/data/globalLongShortAccountRatio":
            payload = [{"longShortRatio": "1.1", "timestamp": end_time - 300_000}]
        elif request.url.path == "/futures/data/takerlongshortRatio":
            payload = [{"buySellRatio": "1.2", "timestamp": end_time - 300_000}]
        else:
            raise AssertionError(request.url.path)
        return httpx.Response(200, json=payload)

    service = SpecialistEvidenceService()
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.test",
        transport=httpx.MockTransport(handler),
    )
    collector = BinanceUsdMDerivativesEvidenceCollector(service, client=client)
    funding_definition = next(
        definition
        for definition in DERIVATIVES_DEFINITIONS
        if definition.metric_name == "funding_rate"
    )
    agent = ExternalEvidenceSpecialist(
        service,
        funding_definition,
        derivatives_collector=collector,
    )
    agent_input = AgentInput(
        correlation_id="derivatives-test",
        agent_name=agent.name,
        timestamp=as_of,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    try:
        output = await agent.run(agent_input)
    finally:
        await client.aclose()

    assert output.signal == Signal.SELL
    assert output.confidence > 0
    assert output.evidence["metric_name"] == "funding_rate"
    assert output.evidence["source"] == "binance.usdm.public-rest"
    assert "MISSING_EVIDENCE" not in output.warnings


@pytest.mark.asyncio
async def test_collector_never_admits_future_provider_rows() -> None:
    as_of = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    future_time = int((as_of + timedelta(minutes=5)).timestamp() * 1_000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/fundingRate":
            payload = [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": future_time,
                    "fundingRate": "0.01",
                }
            ]
        else:
            payload = []
        return httpx.Response(200, json=payload)

    service = SpecialistEvidenceService()
    client = httpx.AsyncClient(
        base_url="https://fapi.binance.test",
        transport=httpx.MockTransport(handler),
    )
    collector = BinanceUsdMDerivativesEvidenceCollector(service, client=client)
    try:
        await collector.refresh(scope="BTCUSDT", as_of=as_of, timeframe="5m")
        evidence = await service.list(
            domain="DERIVATIVES",
            scope="BTCUSDT",
            limit=20,
        )
    finally:
        await client.aclose()

    assert evidence == []

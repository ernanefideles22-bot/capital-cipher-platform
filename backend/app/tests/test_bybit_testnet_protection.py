"""Safety tests for protected Bybit Futures TESTNET submission."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.errors import ExternalServiceError, SecurityError
from app.execution.adapters.bybit_testnet_protected import (
    ProtectedBybitTestnetExecutionAdapter,
)
from app.execution.bybit_protection import BybitOrderProtection
from app.execution.credentials import TestnetCredentials
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    VenuePositionSnapshot,
)


class _ProtectionStore:
    def __init__(self, protection: BybitOrderProtection | None) -> None:
        self.protection = protection

    async def load(self, oms_order_id: str) -> BybitOrderProtection | None:
        if self.protection is None:
            return None
        assert oms_order_id == self.protection.oms_order_id
        return self.protection

    async def aclose(self) -> None:
        return None


def _order() -> OMSOrder:
    return OMSOrder(
        oms_order_id="11111111-1111-1111-1111-111111111111",
        client_order_id="client-123456",
        decision_id="decision-1",
        risk_check_id="risk-1",
        approval_id="a" * 64,
        request_fingerprint="f" * 64,
        correlation_id="correlation-1",
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        timeframe="15m",
        strategy="PROTECTED_TEST",
        side=OrderSide.BUY,
        quantity=0.001,
        requested_notional=100.0,
        leverage=2.0,
        reference_price=100_000.0,
    )


def _protection(order: OMSOrder) -> BybitOrderProtection:
    return BybitOrderProtection(
        oms_order_id=order.oms_order_id,
        approval_id=order.approval_id,
        exchange=order.exchange.value,
        environment=order.environment.value,
        execution_intent="ENTRY",
        reduce_only=False,
        stop_loss=98_000.0,
        take_profit=104_000.0,
        leverage=order.leverage,
    )


def _credentials() -> TestnetCredentials:
    return TestnetCredentials(
        key_id="test-key-123",
        signing_secret="test-secret-1234567890",
    )


@pytest.mark.asyncio
async def test_bybit_entry_without_durable_protection_is_blocked():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"retCode": 1})

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(None),
    )

    with pytest.raises(SecurityError, match="no durable protection evidence"):
        await adapter.submit_order(_order())

    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_bybit_entry_sets_leverage_and_submits_full_market_tpsl():
    order = _order()
    captured: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured.append((request.url.path, body))
        if request.url.path == "/v5/position/set-leverage":
            return httpx.Response(200, json={"retCode": 0, "result": {}})
        if request.url.path == "/v5/order/create":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "orderId": "venue-order-1",
                        "orderLinkId": order.client_order_id,
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.url.path}")

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(_protection(order)),
        clock_ms=lambda: 1_700_000_000_000,
    )

    snapshot = await adapter.submit_order(order)

    assert snapshot.venue_order_id == "venue-order-1"
    assert [path for path, _ in captured] == [
        "/v5/position/set-leverage",
        "/v5/order/create",
    ]
    leverage_body = captured[0][1]
    assert leverage_body["buyLeverage"] == "2"
    assert leverage_body["sellLeverage"] == "2"
    order_body = captured[1][1]
    assert order_body["reduceOnly"] is False
    assert order_body["stopLoss"] == "98000"
    assert order_body["takeProfit"] == "104000"
    assert order_body["tpslMode"] == "Full"
    assert order_body["slOrderType"] == "Market"
    assert order_body["tpOrderType"] == "Market"
    assert order_body["slTriggerBy"] == "MarkPrice"
    assert order_body["tpTriggerBy"] == "MarkPrice"
    await client.aclose()


@pytest.mark.asyncio
async def test_bybit_existing_leverage_code_110043_is_idempotent():
    order = _order()
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v5/position/set-leverage":
            return httpx.Response(
                200,
                json={"retCode": 110043, "retMsg": "Set leverage not modified"},
            )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "orderId": "venue-order-2",
                    "orderLinkId": order.client_order_id,
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(_protection(order)),
    )

    snapshot = await adapter.submit_order(order)

    assert snapshot.venue_order_id == "venue-order-2"
    assert paths == ["/v5/position/set-leverage", "/v5/order/create"]
    await client.aclose()


@pytest.mark.asyncio
async def test_bybit_account_equity_is_read_from_testnet_wallet():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/account/wallet-balance"
        assert request.url.params["accountType"] == "UNIFIED"
        assert request.url.params["coin"] == "USDT"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "coin": [
                                {
                                    "coin": "USDT",
                                    "equity": "12543.21",
                                }
                            ]
                        }
                    ]
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(None),
    )

    equity = await adapter.fetch_account_equity()

    assert equity == pytest.approx(12_543.21)
    await client.aclose()


@pytest.mark.asyncio
async def test_bybit_account_equity_fails_closed_when_wallet_is_ambiguous():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": []},
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(None),
    )

    with pytest.raises(ExternalServiceError, match="ambiguous USDT equity"):
        await adapter.fetch_account_equity()
    await client.aclose()


@pytest.mark.asyncio
async def test_bybit_reduce_only_exit_cannot_increase_exposure():
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v5/order/create"
        body = json.loads(request.content.decode())
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "orderId": "reduce-order-1",
                    "orderLinkId": body["orderLinkId"],
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api-testnet.bybit.com",
        transport=httpx.MockTransport(handler),
    )
    adapter = ProtectedBybitTestnetExecutionAdapter(
        _credentials(),
        client=client,
        protection_store=_ProtectionStore(None),
    )
    position = VenuePositionSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.003,
        entry_price=100_000.0,
        mark_price=101_000.0,
    )

    snapshot = await adapter.submit_reduce_only_exit(
        position,
        client_order_id="cc-exit-123456",
    )

    assert snapshot.side == OrderSide.SELL
    assert snapshot.quantity == pytest.approx(0.003)
    assert len(captured) == 1
    body = captured[0]
    assert body["side"] == "Sell"
    assert body["qty"] == "0.003"
    assert body["reduceOnly"] is True
    assert body["orderType"] == "Market"
    assert body["timeInForce"] == "IOC"
    assert "takeProfit" not in body
    assert "stopLoss" not in body
    await client.aclose()

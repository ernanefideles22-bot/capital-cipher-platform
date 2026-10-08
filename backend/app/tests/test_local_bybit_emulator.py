from __future__ import annotations

import pytest

from app.core.errors import ExecutionRejectedError, SecurityError
from app.execution.adapters.local_bybit_emulator import LocalBybitTestnetEmulator
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
)


def _order(*, side: OrderSide = OrderSide.BUY, quantity: float = 2.0) -> OMSOrder:
    return OMSOrder(
        client_order_id="local-test-0001",
        decision_id="decision-1",
        risk_check_id="risk-1",
        approval_id="a" * 64,
        request_fingerprint="b" * 64,
        correlation_id="corr-1",
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        timeframe="15m",
        strategy="sandbox",
        side=side,
        order_type=OMSOrderType.MARKET,
        quantity=quantity,
        requested_notional=quantity * 100.0,
        leverage=1.0,
        reference_price=100.0,
    )


@pytest.mark.asyncio
async def test_emulator_full_fill_updates_order_position_balance_and_fill():
    adapter = LocalBybitTestnetEmulator(initial_equity=25_000)
    order = _order()

    submitted = await adapter.submit_order(order)
    state = await adapter.fetch_state(symbols={"BTCUSDT"})

    assert submitted.status == OMSOrderStatus.FILLED
    assert submitted.cumulative_filled_quantity == 2.0
    assert submitted.average_fill_price == 100.0
    assert len(state.orders) == 1
    assert len(state.fills) == 1
    assert len(state.positions) == 1
    assert state.positions[0].side == OrderSide.BUY
    assert state.positions[0].quantity == 2.0
    assert state.positions[0].entry_price == 100.0
    assert state.balances[0].asset == "USDT"
    assert state.balances[0].equity == 25_000


@pytest.mark.asyncio
async def test_emulator_partial_fill_is_monotonic_and_cancel_preserves_exposure():
    adapter = LocalBybitTestnetEmulator(default_fill_fraction=0.25)
    order = _order(quantity=4.0)

    first = await adapter.submit_order(order)
    second = adapter.apply_additional_fill(
        venue_order_id=first.venue_order_id,
        quantity=1.5,
        price=102.0,
    )
    cancel_request = order.model_copy(update={"venue_order_id": first.venue_order_id})
    canceled = await adapter.cancel_order(cancel_request)
    state = await adapter.fetch_state(symbols={"BTCUSDT"})

    assert first.status == OMSOrderStatus.PARTIALLY_FILLED
    assert first.cumulative_filled_quantity == 1.0
    assert second.status == OMSOrderStatus.PARTIALLY_FILLED
    assert second.cumulative_filled_quantity == 2.5
    assert canceled.status == OMSOrderStatus.CANCELED
    assert canceled.cumulative_filled_quantity == 2.5
    assert sum(fill.quantity for fill in state.fills) == 2.5
    assert state.positions[0].quantity == 2.5


@pytest.mark.asyncio
async def test_emulator_duplicate_submit_is_idempotent():
    adapter = LocalBybitTestnetEmulator()
    order = _order()

    first = await adapter.submit_order(order)
    second = await adapter.submit_order(order)
    state = await adapter.fetch_state()

    assert second.venue_order_id == first.venue_order_id
    assert len(state.orders) == 1
    assert len(state.fills) == 1
    assert len(state.positions) == 1
    assert state.positions[0].quantity == 2.0


@pytest.mark.asyncio
async def test_emulator_rejects_cross_scope_and_leverage_above_one():
    adapter = LocalBybitTestnetEmulator(default_fill_fraction=0)
    wrong_exchange = _order().model_copy(update={"exchange": Exchange.BINANCE})
    leveraged = _order().model_copy(update={"leverage": 2.0})

    with pytest.raises(SecurityError):
        await adapter.submit_order(wrong_exchange)
    with pytest.raises(SecurityError):
        await adapter.submit_order(leveraged)


@pytest.mark.asyncio
async def test_emulator_rejects_fill_beyond_remaining_quantity():
    adapter = LocalBybitTestnetEmulator(default_fill_fraction=0.5)
    first = await adapter.submit_order(_order(quantity=2.0))

    with pytest.raises(ExecutionRejectedError):
        adapter.apply_additional_fill(
            venue_order_id=first.venue_order_id,
            quantity=1.1,
            price=100.0,
        )


@pytest.mark.asyncio
async def test_emulator_healthcheck_closes_fail_closed():
    adapter = LocalBybitTestnetEmulator()
    assert await adapter.healthcheck() is True
    await adapter.aclose()
    assert await adapter.healthcheck() is False
    with pytest.raises(ExecutionRejectedError):
        await adapter.submit_order(_order())

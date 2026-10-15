"""Explicit safety invariants for TESTNET partial fills and reconciliation."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.oms.reconciliation import _compare_orders, _compare_positions
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    ReconciliationMismatchType,
    ReconciliationSeverity,
    TERMINAL_OMS_STATUSES,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)


def _local(*, filled: float, status=OMSOrderStatus.PARTIALLY_FILLED) -> OMSOrder:
    return OMSOrder(
        client_order_id='cc-partial-fill-test-0001',
        decision_id='decision',
        risk_check_id='risk',
        approval_id='a' * 64,
        request_fingerprint='b' * 64,
        correlation_id='correlation',
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        symbol='BTCUSDT',
        timeframe='15m',
        strategy='TEST',
        side=OrderSide.BUY,
        order_type=OMSOrderType.MARKET,
        quantity=1.0,
        requested_notional=100_000.0,
        reference_price=100_000.0,
        status=status,
        venue_order_id='venue-1',
        cumulative_filled_quantity=filled,
        average_fill_price=100_000.0 if filled else None,
    )


def _venue(*, filled: float, status=OMSOrderStatus.PARTIALLY_FILLED) -> VenueOrderSnapshot:
    return VenueOrderSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        venue_order_id='venue-1',
        client_order_id='cc-partial-fill-test-0001',
        symbol='BTCUSDT',
        side=OrderSide.BUY,
        order_type=OMSOrderType.MARKET,
        status=status,
        quantity=1.0,
        cumulative_filled_quantity=filled,
        average_fill_price=100_100.0 if filled else None,
    )


def _snapshot(order: VenueOrderSnapshot, *, position_quantity: float | None = None):
    positions = []
    if position_quantity is not None:
        positions.append(
            VenuePositionSnapshot(
                exchange=Exchange.BYBIT,
                environment=ExecutionEnvironment.TESTNET,
                symbol='BTCUSDT',
                side=OrderSide.BUY,
                quantity=position_quantity,
                entry_price=100_000.0,
                mark_price=100_100.0,
            )
        )
    return VenueStateSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        orders=[order],
        positions=positions,
        observed_at=datetime.now(timezone.utc),
    )


def test_partially_filled_is_non_terminal():
    assert OMSOrderStatus.PARTIALLY_FILLED not in TERMINAL_OMS_STATUSES
    order = _local(filled=0.25)
    assert order.terminal_at is None


def test_order_and_venue_reject_fill_above_requested_quantity():
    with pytest.raises(ValidationError, match='filled quantity'):
        _local(filled=1.01)
    with pytest.raises(ValidationError, match='filled quantity'):
        _venue(filled=1.01)


def test_reconciliation_advances_partial_fill_monotonically():
    local = _local(filled=0.25)
    mismatches, reconciled = _compare_orders(
        [local], _snapshot(_venue(filled=0.60))
    )
    assert any(
        item.mismatch_type == ReconciliationMismatchType.FILLED_QUANTITY_DRIFT
        and item.severity == ReconciliationSeverity.WARNING
        for item in mismatches
    )
    assert len(reconciled) == 1
    assert reconciled[0].status == OMSOrderStatus.PARTIALLY_FILLED
    assert reconciled[0].cumulative_filled_quantity == 0.60
    assert reconciled[0].terminal_at is None


def test_reconciliation_never_regresses_persisted_fill_and_marks_critical():
    local = _local(filled=0.60)
    mismatches, reconciled = _compare_orders(
        [local], _snapshot(_venue(filled=0.25))
    )
    drift = next(
        item for item in mismatches
        if item.mismatch_type == ReconciliationMismatchType.FILLED_QUANTITY_DRIFT
    )
    assert drift.severity == ReconciliationSeverity.CRITICAL
    assert len(reconciled) == 1
    assert reconciled[0].cumulative_filled_quantity == 0.60


def test_cancel_after_partial_fill_preserves_filled_exposure():
    local = _local(filled=0.40)
    mismatches, reconciled = _compare_orders(
        [local], _snapshot(
            _venue(filled=0.40, status=OMSOrderStatus.CANCELED)
        )
    )
    assert any(
        item.mismatch_type == ReconciliationMismatchType.ORDER_STATUS_DRIFT
        for item in mismatches
    )
    assert len(reconciled) == 1
    assert reconciled[0].status == OMSOrderStatus.CANCELED
    assert reconciled[0].cumulative_filled_quantity == 0.40
    assert reconciled[0].terminal_at is not None


def test_position_inconsistent_with_partial_fill_is_critical_drift():
    local = _local(filled=0.40)
    snapshot = _snapshot(_venue(filled=0.40), position_quantity=0.10)
    mismatches = _compare_positions(
        run_exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        local_orders=[local],
        snapshot=snapshot,
        run_id='run-1',
    )
    mismatch = next(
        item for item in mismatches
        if item.mismatch_type == ReconciliationMismatchType.POSITION_QUANTITY_DRIFT
    )
    assert mismatch.severity == ReconciliationSeverity.CRITICAL

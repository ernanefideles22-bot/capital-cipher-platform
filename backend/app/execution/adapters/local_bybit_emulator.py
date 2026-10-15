"""Deterministic, no-network Bybit Futures TESTNET emulator.

This adapter exists only for local/CI validation of OMS and reconciliation paths.
It deliberately uses the TESTNET execution contract while performing no remote
API calls and holding no credentials. It is not wired into the hosted runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.core.errors import ExecutionRejectedError, SecurityError
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.schemas.common import Exchange, OrderSide, utcnow
from app.schemas.oms import (
    ExecutionEnvironment,
    ExecutionFill,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    TERMINAL_OMS_STATUSES,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)


@dataclass
class _EmulatedPosition:
    signed_quantity: float = 0.0
    entry_price: float | None = None


class LocalBybitTestnetEmulator(ExchangeExecutionAdapter):
    """Small stateful exchange emulator for deterministic TESTNET-path tests."""

    exchange = Exchange.BYBIT

    def __init__(
        self,
        *,
        initial_equity: float = 10_000.0,
        default_fill_fraction: float = 1.0,
    ) -> None:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if not 0 <= default_fill_fraction <= 1:
            raise ValueError("default_fill_fraction must be between 0 and 1")
        self._equity = float(initial_equity)
        self._default_fill_fraction = float(default_fill_fraction)
        self._orders: dict[str, VenueOrderSnapshot] = {}
        self._fills: dict[str, ExecutionFill] = {}
        self._positions: dict[str, _EmulatedPosition] = {}
        self._closed = False

    async def healthcheck(self) -> bool:
        return not self._closed

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._ensure_open()
        self._validate_order(order)
        existing = next(
            (
                item
                for item in self._orders.values()
                if item.client_order_id == order.client_order_id
            ),
            None,
        )
        if existing is not None:
            return existing

        venue_order_id = f"local-bybit-{uuid4().hex[:20]}"
        snapshot = VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=venue_order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=OMSOrderStatus.SUBMITTED,
            quantity=order.quantity,
            cumulative_filled_quantity=0.0,
        )
        self._orders[venue_order_id] = snapshot
        if self._default_fill_fraction > 0:
            self._apply_fill(
                snapshot,
                quantity=order.quantity * self._default_fill_fraction,
                price=order.limit_price or order.reference_price,
            )
        return self._orders[venue_order_id]

    async def submit_reduce_only_exit(
        self,
        position: VenuePositionSnapshot,
        *,
        client_order_id: str,
    ) -> VenueOrderSnapshot:
        self._ensure_open()
        if (
            position.exchange != self.exchange
            or position.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError("Reduce-only exit is not scoped to Bybit TESTNET")
        if position.quantity <= 0:
            raise SecurityError("Reduce-only exit requires positive quantity")
        normalized_client_id = client_order_id.strip()
        if not 8 <= len(normalized_client_id) <= 36:
            raise SecurityError("Reduce-only exit requires bounded client order id")

        existing = next(
            (
                item
                for item in self._orders.values()
                if item.client_order_id == normalized_client_id
            ),
            None,
        )
        if existing is not None:
            return existing

        state = self._positions.get(position.symbol)
        if state is None or abs(state.signed_quantity) <= 1e-12:
            raise ExecutionRejectedError("Reduce-only exit found no open position")
        venue_side = OrderSide.BUY if state.signed_quantity > 0 else OrderSide.SELL
        if venue_side != position.side:
            raise ExecutionRejectedError("Reduce-only exit side does not match position")
        if position.quantity > abs(state.signed_quantity) + 1e-12:
            raise ExecutionRejectedError("Reduce-only exit exceeds open exposure")

        close_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        price = position.mark_price or position.entry_price
        if price is None or price <= 0:
            raise ExecutionRejectedError("Reduce-only exit requires a valid venue price")

        venue_order_id = f"local-bybit-exit-{uuid4().hex[:16]}"
        snapshot = VenueOrderSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            venue_order_id=venue_order_id,
            client_order_id=normalized_client_id,
            symbol=position.symbol,
            side=close_side,
            order_type=OMSOrderType.MARKET,
            status=OMSOrderStatus.FILLED,
            quantity=position.quantity,
            cumulative_filled_quantity=position.quantity,
            average_fill_price=price,
        )
        self._orders[venue_order_id] = snapshot
        fill_id = f"{venue_order_id}:{position.quantity:.12f}"
        self._fills.setdefault(
            fill_id,
            ExecutionFill(
                fill_id=fill_id,
                venue_order_id=venue_order_id,
                client_order_id=normalized_client_id,
                exchange=self.exchange,
                environment=ExecutionEnvironment.TESTNET,
                symbol=position.symbol,
                side=close_side,
                quantity=position.quantity,
                price=price,
                fee=0.0,
                fee_asset="USDT",
                occurred_at=utcnow(),
            ),
        )
        self._apply_position_delta(
            symbol=position.symbol,
            side=close_side,
            quantity=position.quantity,
            price=price,
        )
        return snapshot

    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        self._ensure_open()
        self._validate_order(order)
        snapshot = self._find_order(order)
        if snapshot.status in TERMINAL_OMS_STATUSES:
            return snapshot
        canceled = snapshot.model_copy(
            update={
                "status": OMSOrderStatus.CANCELED,
                "observed_at": utcnow(),
            }
        )
        self._orders[canceled.venue_order_id] = canceled
        return canceled

    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        self._ensure_open()
        requested = {item.upper() for item in symbols} if symbols else None
        orders = [
            item
            for item in self._orders.values()
            if requested is None or item.symbol.upper() in requested
        ]
        fills = [
            item
            for item in self._fills.values()
            if requested is None or item.symbol.upper() in requested
        ]
        positions: list[VenuePositionSnapshot] = []
        for symbol, state in self._positions.items():
            if requested is not None and symbol.upper() not in requested:
                continue
            if abs(state.signed_quantity) <= 1e-12:
                continue
            side = OrderSide.BUY if state.signed_quantity > 0 else OrderSide.SELL
            positions.append(
                VenuePositionSnapshot(
                    exchange=self.exchange,
                    environment=ExecutionEnvironment.TESTNET,
                    symbol=symbol,
                    side=side,
                    quantity=abs(state.signed_quantity),
                    entry_price=state.entry_price,
                    mark_price=state.entry_price,
                    unrealized_pnl=0.0,
                )
            )
        balance = VenueBalanceSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            asset="USDT",
            available=self._equity,
            locked=0.0,
            equity=self._equity,
        )
        return VenueStateSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.TESTNET,
            orders=orders,
            fills=fills,
            positions=positions,
            balances=[balance],
        )

    async def aclose(self) -> None:
        self._closed = True

    def apply_additional_fill(
        self,
        *,
        venue_order_id: str,
        quantity: float,
        price: float,
    ) -> VenueOrderSnapshot:
        """Test-only control to emulate later partial/full venue fills."""

        self._ensure_open()
        snapshot = self._orders[venue_order_id]
        return self._apply_fill(snapshot, quantity=quantity, price=price)

    def _apply_fill(
        self,
        snapshot: VenueOrderSnapshot,
        *,
        quantity: float,
        price: float,
    ) -> VenueOrderSnapshot:
        if snapshot.status in TERMINAL_OMS_STATUSES:
            raise ExecutionRejectedError(
                f"Cannot apply fill to terminal emulated order {snapshot.status.value}"
            )
        if quantity <= 0 or price <= 0:
            raise ValueError("fill quantity and price must be positive")
        remaining = snapshot.quantity - snapshot.cumulative_filled_quantity
        if quantity > remaining + 1e-12:
            raise ExecutionRejectedError("Emulated fill exceeds remaining quantity")
        cumulative = snapshot.cumulative_filled_quantity + quantity
        previous_notional = (
            snapshot.cumulative_filled_quantity * (snapshot.average_fill_price or 0.0)
        )
        average = (previous_notional + quantity * price) / cumulative
        status = (
            OMSOrderStatus.FILLED
            if abs(cumulative - snapshot.quantity) <= 1e-12
            else OMSOrderStatus.PARTIALLY_FILLED
        )
        updated = snapshot.model_copy(
            update={
                "status": status,
                "cumulative_filled_quantity": cumulative,
                "average_fill_price": average,
                "observed_at": utcnow(),
            }
        )
        self._orders[updated.venue_order_id] = updated
        fill_id = f"{updated.venue_order_id}:{cumulative:.12f}"
        self._fills.setdefault(
            fill_id,
            ExecutionFill(
                fill_id=fill_id,
                venue_order_id=updated.venue_order_id,
                client_order_id=updated.client_order_id,
                exchange=self.exchange,
                environment=ExecutionEnvironment.TESTNET,
                symbol=updated.symbol,
                side=updated.side,
                quantity=quantity,
                price=price,
                fee=0.0,
                fee_asset="USDT",
                occurred_at=utcnow(),
            ),
        )
        self._apply_position_delta(
            symbol=updated.symbol,
            side=updated.side,
            quantity=quantity,
            price=price,
        )
        return updated

    def _apply_position_delta(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: float,
        price: float,
    ) -> None:
        state = self._positions.setdefault(symbol, _EmulatedPosition())
        signed_delta = quantity if side == OrderSide.BUY else -quantity
        before = state.signed_quantity
        after = before + signed_delta
        if abs(after) <= 1e-12:
            state.signed_quantity = 0.0
            state.entry_price = None
            return
        if before == 0 or before * signed_delta > 0:
            previous_notional = abs(before) * (state.entry_price or price)
            state.entry_price = (
                previous_notional + abs(signed_delta) * price
            ) / abs(after)
        elif before * after < 0:
            state.entry_price = price
        state.signed_quantity = after

    def _find_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        if order.venue_order_id:
            snapshot = self._orders.get(order.venue_order_id)
            if snapshot is None:
                raise ExecutionRejectedError("Emulated order not found")
            if snapshot.client_order_id != order.client_order_id:
                raise ExecutionRejectedError(
                    "Emulated venue and client order identifiers do not match"
                )
            return snapshot
        for snapshot in self._orders.values():
            if snapshot.client_order_id == order.client_order_id:
                return snapshot
        raise ExecutionRejectedError("Emulated order not found")

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutionRejectedError("Local TESTNET emulator is closed")

    def _validate_order(self, order: OMSOrder) -> None:
        if (
            order.exchange != self.exchange
            or order.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError("Order is not scoped to Bybit TESTNET")
        if order.leverage != 1:
            raise SecurityError("Local TESTNET emulator permits only 1x leverage")

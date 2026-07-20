"""Read-only PAPER venue view used by the OMS reconciler."""

from __future__ import annotations

from app.core.errors import SecurityError
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.paper_trading.engine import PaperTradingEngine
from app.schemas.common import Exchange, PaperOrderStatus, utcnow
from app.schemas.oms import (
    ExecutionEnvironment,
    ExecutionFill,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)


class PaperExecutionAdapter(ExchangeExecutionAdapter):
    """Expose simulator state without providing a second submission path."""

    environment = ExecutionEnvironment.PAPER

    def __init__(
        self,
        engine: PaperTradingEngine,
        *,
        exchange: Exchange = Exchange.BINANCE,
    ) -> None:
        self.exchange = exchange
        self._engine = engine

    async def healthcheck(self) -> bool:
        return True

    async def submit_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        raise SecurityError(
            "PAPER submission must enter through PaperTradingEngine risk checks"
        )

    async def cancel_order(self, order: OMSOrder) -> VenueOrderSnapshot:
        raise SecurityError(
            "PAPER cancellation must enter through PaperTradingEngine"
        )

    async def fetch_state(
        self,
        *,
        symbols: set[str] | None = None,
    ) -> VenueStateSnapshot:
        observed_at = utcnow()
        paper_orders = [
            *self._engine.open_orders.values(),
            *self._engine.closed_orders,
        ]
        if symbols:
            allowed = {symbol.upper() for symbol in symbols}
            paper_orders = [
                order for order in paper_orders if order.symbol.upper() in allowed
            ]
        orders = [
            VenueOrderSnapshot(
                exchange=order.exchange,
                environment=ExecutionEnvironment.PAPER,
                venue_order_id=order.paper_order_id,
                client_order_id=_paper_client_order_id(order.approval_id),
                symbol=order.symbol,
                side=order.side,
                order_type=OMSOrderType.MARKET,
                status=_paper_status(order.status),
                quantity=order.position_size / order.entry_price,
                cumulative_filled_quantity=order.position_size
                / order.entry_price,
                average_fill_price=order.entry_price,
                observed_at=observed_at,
            )
            for order in paper_orders
        ]
        fills = [
            ExecutionFill(
                fill_id=f"paper:{order.paper_order_id}:open",
                oms_order_id=order.paper_order_id,
                venue_order_id=order.paper_order_id,
                client_order_id=_paper_client_order_id(order.approval_id),
                exchange=order.exchange,
                environment=ExecutionEnvironment.PAPER,
                symbol=order.symbol,
                side=order.side,
                quantity=order.position_size / order.entry_price,
                price=order.entry_price,
                fee=order.fees_estimated,
                occurred_at=order.opened_at or order.created_at,
                observed_at=observed_at,
            )
            for order in paper_orders
            if order.status
            in {
                PaperOrderStatus.FILLED,
                PaperOrderStatus.PARTIALLY_FILLED,
                PaperOrderStatus.CLOSED,
            }
        ]
        positions = [
            VenuePositionSnapshot(
                exchange=order.exchange,
                environment=ExecutionEnvironment.PAPER,
                symbol=order.symbol,
                side=order.side,
                quantity=order.position_size / order.entry_price,
                entry_price=order.entry_price,
                mark_price=order.entry_price,
                observed_at=observed_at,
            )
            for order in self._engine.open_orders.values()
            if not symbols or order.symbol.upper() in {s.upper() for s in symbols}
        ]
        balance = max(0.0, self._engine.balance)
        return VenueStateSnapshot(
            exchange=self.exchange,
            environment=ExecutionEnvironment.PAPER,
            orders=orders,
            fills=fills,
            positions=positions,
            balances=[
                VenueBalanceSnapshot(
                    exchange=self.exchange,
                    environment=ExecutionEnvironment.PAPER,
                    asset="USDT",
                    available=balance,
                    equity=balance,
                    observed_at=observed_at,
                )
            ],
            observed_at=observed_at,
        )

    async def aclose(self) -> None:
        return None


def _paper_client_order_id(approval_id: str | None) -> str:
    return f"paper-{(approval_id or 'legacy')[:24]}"


def _paper_status(status: PaperOrderStatus) -> OMSOrderStatus:
    return {
        PaperOrderStatus.CREATED: OMSOrderStatus.CREATED,
        PaperOrderStatus.FILLED: OMSOrderStatus.FILLED,
        PaperOrderStatus.PARTIALLY_FILLED: OMSOrderStatus.PARTIALLY_FILLED,
        PaperOrderStatus.CANCELLED: OMSOrderStatus.CANCELED,
        PaperOrderStatus.CLOSED: OMSOrderStatus.FILLED,
        PaperOrderStatus.EXPIRED: OMSOrderStatus.EXPIRED,
        PaperOrderStatus.FAILED: OMSOrderStatus.REJECTED,
    }[status]

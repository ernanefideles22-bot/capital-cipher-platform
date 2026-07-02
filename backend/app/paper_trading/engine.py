"""Paper Trading Engine (docs/18-paper-trading.md, ADR-004 paper-only).

Simulates orders with estimated fees, slippage and spread. A paper order can
only exist with an APPROVED or REDUCED risk check (docs/18, docs/29 invariants).
No real orders, no private API keys, ever (Phase 1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.audit.service import AuditService
from app.core.errors import RiskError, ValidationError
from app.core.logging import ServiceLogger
from app.risk.manager import RiskManager
from app.schemas.common import Exchange, OrderSide, PaperOrderStatus, RiskStatus
from app.schemas.decisions import Decision
from app.schemas.market import Candle
from app.schemas.paper import EquityPoint, PaperOrder, PaperPerformance, SymbolPerformance
from app.schemas.risk import RiskCheck

logger = ServiceLogger("paper_trading")


class PaperTradingEngine:
    def __init__(
        self,
        audit_service: AuditService,
        risk_manager: RiskManager,
        *,
        initial_balance: float = 10_000.0,
        fee_rate_percent: float = 0.08,
        slippage_rate_percent: float = 0.02,
        repository=None,
    ) -> None:
        self._audit = audit_service
        self._risk = risk_manager
        self._repository = repository
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.fee_rate = fee_rate_percent / 100
        self.slippage_rate = slippage_rate_percent / 100
        self.open_orders: dict[str, PaperOrder] = {}
        self.closed_orders: list[PaperOrder] = []
        self._processed_keys: set[tuple[str, str]] = set()
        self._peak_equity = initial_balance
        self._max_drawdown_percent = 0.0
        self.equity_curve: list[EquityPoint] = [
            EquityPoint(timestamp=datetime.now(timezone.utc).isoformat(), balance=initial_balance)
        ]

    # -- order creation --------------------------------------------------------
    async def create_order(
        self, decision: Decision, risk_check: RiskCheck, *, current_price: float
    ) -> PaperOrder:  # noqa: D401
        # Invariants (docs/18, docs/29): no order without approved risk check.
        if risk_check.risk_status not in (RiskStatus.APPROVED, RiskStatus.REDUCED):
            raise RiskError(
                f"Paper order rejected: risk_status={risk_check.risk_status.value}",
                correlation_id=decision.correlation_id,
            )
        if risk_check.decision_id != decision.decision_id:
            raise ValidationError(
                "Risk check does not belong to this decision",
                correlation_id=decision.correlation_id,
            )
        # Idempotency (docs/23): one order per decision_id + risk_check_id.
        key = (decision.decision_id, risk_check.risk_check_id)
        if key in self._processed_keys:
            raise ValidationError(
                "Duplicate paper order for the same decision and risk check",
                correlation_id=decision.correlation_id,
            )

        side = OrderSide(decision.candidate_action.value)
        slippage_cost = current_price * self.slippage_rate
        entry_price = (
            current_price + slippage_cost if side == OrderSide.BUY else current_price - slippage_cost
        )
        position_size = risk_check.position_size or 0.0
        fees = position_size * self.fee_rate

        order = PaperOrder(
            decision_id=decision.decision_id,
            risk_check_id=risk_check.risk_check_id,
            correlation_id=decision.correlation_id,
            exchange=Exchange.BINANCE,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            side=side,
            entry_price=round(entry_price, 2),
            stop_loss=risk_check.stop_loss,
            take_profit=risk_check.take_profit,
            position_size=position_size,
            status=PaperOrderStatus.FILLED,
            fees_estimated=round(fees, 4),
            slippage_estimated=round(slippage_cost, 4),
            opened_at=datetime.now(timezone.utc),
        )
        # Audit BEFORE accepting the order into the book (docs/12 critical rule).
        await self._audit.record(
            correlation_id=decision.correlation_id,
            audit_type="PAPER_ORDER_CREATED",
            entity_type="paper_order",
            entity_id=order.paper_order_id,
            payload=order.model_dump(mode="json"),
        )
        self._processed_keys.add(key)
        self.open_orders[order.paper_order_id] = order
        self._risk.set_open_positions(len(self.open_orders))
        if self._repository is not None:
            await self._repository.save_paper_order(order)
        logger.info(
            f"Paper order created {order.symbol} {order.side.value}",
            event_type="PAPER_ORDER_CREATED",
            correlation_id=decision.correlation_id,
            metadata={"paper_order_id": order.paper_order_id, "entry": order.entry_price},
        )
        return order

    # -- position monitoring ----------------------------------------------------
    async def on_candle(self, candle: Candle) -> list[PaperOrder]:
        """Check SL/TP for open positions on every closed candle (docs/18)."""
        closed: list[PaperOrder] = []
        for order in list(self.open_orders.values()):
            if order.symbol != candle.symbol:
                continue
            exit_price: float | None = None
            exit_reason: str | None = None
            if order.side == OrderSide.BUY:
                if order.stop_loss is not None and candle.low <= order.stop_loss:
                    exit_price, exit_reason = order.stop_loss, "STOP_LOSS"
                elif order.take_profit is not None and candle.high >= order.take_profit:
                    exit_price, exit_reason = order.take_profit, "TAKE_PROFIT"
            else:
                if order.stop_loss is not None and candle.high >= order.stop_loss:
                    exit_price, exit_reason = order.stop_loss, "STOP_LOSS"
                elif order.take_profit is not None and candle.low <= order.take_profit:
                    exit_price, exit_reason = order.take_profit, "TAKE_PROFIT"
            if exit_price is not None:
                closed.append(await self.close_order(order.paper_order_id, exit_price, exit_reason))
        return closed

    async def close_order(
        self, paper_order_id: str, exit_price: float, exit_reason: str | None = None
    ) -> PaperOrder:
        order = self.open_orders.pop(paper_order_id, None)
        if order is None:
            raise ValidationError(f"Paper order {paper_order_id} is not open")
        exit_fees = order.position_size * self.fee_rate
        qty = order.position_size / order.entry_price
        direction = 1 if order.side == OrderSide.BUY else -1
        gross_pnl = (exit_price - order.entry_price) * qty * direction
        net_pnl = gross_pnl - order.fees_estimated - exit_fees

        closed = order.model_copy(
            update={
                "status": PaperOrderStatus.CLOSED,
                "closed_at": datetime.now(timezone.utc),
                "pnl": round(net_pnl, 4),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "fees_estimated": round(order.fees_estimated + exit_fees, 4),
            }
        )
        self.closed_orders.append(closed)
        self.balance += net_pnl
        self._risk.set_open_positions(len(self.open_orders))
        self._risk.register_trade_result(net_pnl)
        self._peak_equity = max(self._peak_equity, self.balance)
        drawdown = (self._peak_equity - self.balance) / self._peak_equity * 100
        self._max_drawdown_percent = max(self._max_drawdown_percent, drawdown)
        self._risk.update_equity(self.balance)
        self.equity_curve.append(
            EquityPoint(timestamp=datetime.now(timezone.utc).isoformat(), balance=round(self.balance, 4))
        )

        await self._audit.record(
            correlation_id=order.correlation_id,
            audit_type="PAPER_ORDER_CLOSED",
            entity_type="paper_order",
            entity_id=order.paper_order_id,
            payload=closed.model_dump(mode="json"),
        )
        if self._repository is not None:
            await self._repository.save_paper_order(closed)
        logger.info(
            f"Paper order closed {closed.symbol} pnl={closed.pnl}",
            event_type="PAPER_ORDER_CLOSED",
            correlation_id=order.correlation_id,
            metadata={"exit_reason": exit_reason, "pnl": closed.pnl},
        )
        return closed

    # -- reporting ---------------------------------------------------------------
    def performance(self) -> PaperPerformance:
        wins = [o for o in self.closed_orders if (o.pnl or 0) > 0]
        losses = [o for o in self.closed_orders if (o.pnl or 0) <= 0]
        closed_count = len(self.closed_orders)
        gross = sum((o.pnl or 0) + o.fees_estimated for o in self.closed_orders)
        net = sum(o.pnl or 0 for o in self.closed_orders)
        return PaperPerformance(
            total_trades=closed_count + len(self.open_orders),
            open_trades=len(self.open_orders),
            closed_trades=closed_count,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / closed_count * 100, 2) if closed_count else 0.0,
            gross_pnl=round(gross, 4),
            net_pnl=round(net, 4),
            fees_total=round(sum(o.fees_estimated for o in self.closed_orders), 4),
            slippage_total=round(sum(o.slippage_estimated for o in self.closed_orders), 4),
            max_drawdown_percent=round(self._max_drawdown_percent, 4),
            consecutive_losses=self._risk.state.consecutive_losses,
            balance=round(self.balance, 2),
            initial_balance=self.initial_balance,
        )

    def performance_by(self, dimension: str = "symbol") -> list[SymbolPerformance]:
        """Breakdown by symbol or timeframe (docs/07 Fase 2 reports)."""
        groups: dict[str, list] = {}
        for order in self.closed_orders:
            key = order.symbol if dimension == "symbol" else (order.timeframe or "unknown")
            groups.setdefault(key, []).append(order)
        results: list[SymbolPerformance] = []
        for key, orders in sorted(groups.items()):
            wins = [o for o in orders if (o.pnl or 0) > 0]
            losses = [o for o in orders if (o.pnl or 0) <= 0]
            gross_wins = sum(o.pnl or 0 for o in wins)
            gross_losses = abs(sum(o.pnl or 0 for o in losses))
            results.append(
                SymbolPerformance(
                    key=key,
                    trades=len(orders),
                    wins=len(wins),
                    losses=len(losses),
                    win_rate=round(len(wins) / len(orders) * 100, 2) if orders else 0.0,
                    net_pnl=round(sum(o.pnl or 0 for o in orders), 4),
                    profit_factor=round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
                )
            )
        return results

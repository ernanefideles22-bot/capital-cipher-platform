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
from app.paper_trading.execution import (
    ExecutionCostLedger,
    IsolatedMarginModel,
    RealisticExecutionModel,
)
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
        execution_model: RealisticExecutionModel | None = None,
        margin_model: IsolatedMarginModel | None = None,
        started_at: datetime | None = None,
        repository=None,
    ) -> None:
        self._audit = audit_service
        self._risk = risk_manager
        self._repository = repository
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.fee_rate = fee_rate_percent / 100
        self.slippage_rate = slippage_rate_percent / 100
        self._execution_model = execution_model
        self._margin_model = margin_model
        self.open_orders: dict[str, PaperOrder] = {}
        self.closed_orders: list[PaperOrder] = []
        self._execution_costs: dict[str, ExecutionCostLedger] = {}
        self._last_funding_at: dict[str, datetime] = {}
        self._processed_keys: set[tuple[str, str]] = set()
        self._peak_equity = initial_balance
        self._max_drawdown_percent = 0.0
        self.equity_curve: list[EquityPoint] = [
            EquityPoint(
                timestamp=(started_at or datetime.now(timezone.utc)).isoformat(),
                balance=initial_balance,
            )
        ]

    # -- order creation --------------------------------------------------------
    async def create_order(
        self,
        decision: Decision,
        risk_check: RiskCheck,
        *,
        current_price: float,
        market_candle: Candle | None = None,
        occurred_at: datetime | None = None,
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
        position_size = risk_check.position_size or 0.0
        ledger = ExecutionCostLedger()
        if self._execution_model is not None:
            fill = self._execution_model.open_fill(
                side=side,
                reference_price=current_price,
                position_notional=position_size,
                candle=market_candle,
            )
            entry_price = fill.fill_price
            fees = fill.fee_cost
            slippage_estimate = fill.spread_cost + fill.slippage_cost
            ledger.fees = fees
            ledger.spread = fill.spread_cost
            ledger.slippage = fill.slippage_cost
            ledger.volume_impact = fill.volume_impact_cost
            entry_precision = 8
        else:
            slippage_cost = current_price * self.slippage_rate
            entry_price = (
                current_price + slippage_cost
                if side == OrderSide.BUY
                else current_price - slippage_cost
            )
            fees = position_size * self.fee_rate
            slippage_estimate = slippage_cost
            entry_precision = 2
        opened_at = occurred_at or datetime.now(timezone.utc)
        margin_values: dict = {}
        if self._margin_model is not None:
            margin = self._margin_model.assumptions
            margin_values = {
                "leverage": margin.leverage,
                "initial_margin": round(
                    self._margin_model.initial_margin(position_size),
                    8,
                ),
                "maintenance_margin_ratio": (
                    margin.maintenance_margin_ratio
                ),
                "liquidation_price": round(
                    self._margin_model.liquidation_price(
                        side=side,
                        entry_price=entry_price,
                    ),
                    8,
                ),
            }

        order = PaperOrder(
            decision_id=decision.decision_id,
            risk_check_id=risk_check.risk_check_id,
            correlation_id=decision.correlation_id,
            exchange=Exchange.BINANCE,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            side=side,
            entry_price=round(entry_price, entry_precision),
            stop_loss=risk_check.stop_loss,
            take_profit=risk_check.take_profit,
            position_size=position_size,
            status=PaperOrderStatus.FILLED,
            fees_estimated=round(fees, 4),
            slippage_estimated=round(slippage_estimate, 4),
            opened_at=opened_at,
            **margin_values,
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
        if self._execution_model is not None:
            self._execution_costs[order.paper_order_id] = ledger
            self._last_funding_at[order.paper_order_id] = opened_at
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
            self._accrue_funding(order, candle.closed_at)
            exit_price: float | None = None
            exit_reason: str | None = None
            if self._margin_model is not None:
                exit_price = self._margin_model.liquidation_reference(
                    order=order,
                    candle=candle,
                )
                if exit_price is not None:
                    exit_reason = "LIQUIDATION"
            if exit_price is None and order.side == OrderSide.BUY:
                if order.stop_loss is not None and candle.low <= order.stop_loss:
                    exit_price, exit_reason = order.stop_loss, "STOP_LOSS"
                elif order.take_profit is not None and candle.high >= order.take_profit:
                    exit_price, exit_reason = order.take_profit, "TAKE_PROFIT"
            elif exit_price is None:
                if order.stop_loss is not None and candle.high >= order.stop_loss:
                    exit_price, exit_reason = order.stop_loss, "STOP_LOSS"
                elif order.take_profit is not None and candle.low <= order.take_profit:
                    exit_price, exit_reason = order.take_profit, "TAKE_PROFIT"
            if exit_price is not None:
                closed.append(
                    await self.close_order(
                        order.paper_order_id,
                        exit_price,
                        exit_reason,
                        market_candle=candle,
                        occurred_at=candle.closed_at,
                    )
                )
        return closed

    async def close_order(
        self,
        paper_order_id: str,
        exit_price: float,
        exit_reason: str | None = None,
        *,
        market_candle: Candle | None = None,
        occurred_at: datetime | None = None,
    ) -> PaperOrder:
        if exit_price <= 0:
            raise ValidationError("Paper order exit price must be positive")
        order = self.open_orders.get(paper_order_id)
        if order is None:
            raise ValidationError(f"Paper order {paper_order_id} is not open")
        qty = order.position_size / order.entry_price
        if (
            self._execution_model is not None
            and occurred_at is not None
        ):
            self._accrue_funding(order, occurred_at)
        stored_ledger = self._execution_costs.get(
            paper_order_id,
            ExecutionCostLedger(),
        )
        ledger = ExecutionCostLedger(
            fees=stored_ledger.fees,
            spread=stored_ledger.spread,
            slippage=stored_ledger.slippage,
            volume_impact=stored_ledger.volume_impact,
            funding=stored_ledger.funding,
            liquidation_fees=stored_ledger.liquidation_fees,
        )
        if self._execution_model is not None:
            fill = self._execution_model.close_fill(
                position_side=order.side,
                reference_price=exit_price,
                quantity=qty,
                candle=market_candle,
            )
            exit_price = fill.fill_price
            exit_fees = fill.fee_cost
            ledger.fees += exit_fees
            ledger.spread += fill.spread_cost
            ledger.slippage += fill.slippage_cost
            ledger.volume_impact += fill.volume_impact_cost
            slippage_estimate = ledger.spread + ledger.slippage
        else:
            exit_fees = order.position_size * self.fee_rate
            slippage_estimate = order.slippage_estimated
        liquidation_fee = 0.0
        if (
            exit_reason == "LIQUIDATION"
            and self._margin_model is not None
        ):
            liquidation_fee = self._margin_model.liquidation_fee(
                qty * exit_price
            )
            ledger.liquidation_fees += liquidation_fee
        direction = 1 if order.side == OrderSide.BUY else -1
        gross_pnl = (exit_price - order.entry_price) * qty * direction
        net_pnl = (
            gross_pnl
            - order.fees_estimated
            - exit_fees
            - ledger.funding
            - liquidation_fee
        )
        closed_at = occurred_at or datetime.now(timezone.utc)

        closed = order.model_copy(
            update={
                "status": PaperOrderStatus.CLOSED,
                "closed_at": closed_at,
                "pnl": round(net_pnl, 4),
                "exit_price": round(exit_price, 8),
                "exit_reason": exit_reason,
                "fees_estimated": round(order.fees_estimated + exit_fees, 4),
                "slippage_estimated": round(slippage_estimate, 4),
                "liquidation_fee": round(liquidation_fee, 4),
            }
        )
        # Record the terminal state before it advances the in-memory account.
        await self._audit.record(
            correlation_id=order.correlation_id,
            audit_type="PAPER_ORDER_CLOSED",
            entity_type="paper_order",
            entity_id=order.paper_order_id,
            payload=closed.model_dump(mode="json"),
        )
        if self._repository is not None:
            await self._repository.save_paper_order(closed)

        if self._execution_model is not None:
            self._execution_costs[paper_order_id] = ledger
            self._last_funding_at.pop(paper_order_id, None)
        self.open_orders.pop(paper_order_id)
        self.closed_orders.append(closed)
        self.balance += net_pnl
        self._risk.set_open_positions(len(self.open_orders))
        self._risk.register_trade_result(net_pnl)
        self._peak_equity = max(self._peak_equity, self.balance)
        drawdown = (self._peak_equity - self.balance) / self._peak_equity * 100
        self._max_drawdown_percent = max(self._max_drawdown_percent, drawdown)
        self._risk.update_equity(self.balance)
        self.equity_curve.append(
            EquityPoint(
                timestamp=closed_at.isoformat(),
                balance=round(self.balance, 4),
            )
        )

        logger.info(
            f"Paper order closed {closed.symbol} pnl={closed.pnl}",
            event_type="PAPER_ORDER_CLOSED",
            correlation_id=order.correlation_id,
            metadata={"exit_reason": exit_reason, "pnl": closed.pnl},
        )
        return closed

    def _accrue_funding(
        self,
        order: PaperOrder,
        end_at: datetime,
    ) -> None:
        if self._execution_model is None:
            return
        start_at = self._last_funding_at.get(order.paper_order_id)
        if start_at is None or end_at <= start_at:
            return
        ledger = self._execution_costs[order.paper_order_id]
        ledger.funding += self._execution_model.funding_cost(
            order=order,
            start_at=start_at,
            end_at=end_at,
        )
        self._last_funding_at[order.paper_order_id] = end_at

    # -- reporting ---------------------------------------------------------------
    def performance(self) -> PaperPerformance:
        wins = [o for o in self.closed_orders if (o.pnl or 0) > 0]
        losses = [o for o in self.closed_orders if (o.pnl or 0) <= 0]
        closed_count = len(self.closed_orders)
        funding_total = sum(
            self._execution_costs.get(
                order.paper_order_id,
                ExecutionCostLedger(),
            ).funding
            for order in self.closed_orders
        )
        spread_total = sum(
            self._execution_costs.get(
                order.paper_order_id,
                ExecutionCostLedger(),
            ).spread
            for order in self.closed_orders
        )
        slippage_total = sum(
            self._execution_costs.get(
                order.paper_order_id,
                ExecutionCostLedger(),
            ).slippage
            for order in self.closed_orders
        )
        volume_impact_total = sum(
            self._execution_costs.get(
                order.paper_order_id,
                ExecutionCostLedger(),
            ).volume_impact
            for order in self.closed_orders
        )
        liquidation_fees_total = sum(
            self._execution_costs.get(
                order.paper_order_id,
                ExecutionCostLedger(),
            ).liquidation_fees
            for order in self.closed_orders
        )
        liquidations = sum(
            order.exit_reason == "LIQUIDATION"
            for order in self.closed_orders
        )
        gross = sum(
            (o.pnl or 0)
            + (
                self._execution_costs.get(
                    o.paper_order_id,
                    ExecutionCostLedger(),
                ).fees
                if self._execution_model is not None
                else o.fees_estimated
            )
            + self._execution_costs.get(
                o.paper_order_id,
                ExecutionCostLedger(),
            ).funding
            + self._execution_costs.get(
                o.paper_order_id,
                ExecutionCostLedger(),
            ).liquidation_fees
            for o in self.closed_orders
        )
        net = sum(o.pnl or 0 for o in self.closed_orders)
        if self._execution_model is None:
            fees_total = sum(
                o.fees_estimated for o in self.closed_orders
            )
            slippage_total = sum(
                o.slippage_estimated for o in self.closed_orders
            )
        else:
            fees_total = sum(
                self._execution_costs.get(
                    order.paper_order_id,
                    ExecutionCostLedger(),
                ).fees
                for order in self.closed_orders
            )
        rounded_fees = round(fees_total, 4)
        rounded_slippage = round(slippage_total, 4)
        rounded_spread = round(spread_total, 4)
        rounded_volume_impact = round(volume_impact_total, 4)
        rounded_funding = round(funding_total, 4)
        rounded_liquidation_fees = round(liquidation_fees_total, 4)
        return PaperPerformance(
            total_trades=closed_count + len(self.open_orders),
            open_trades=len(self.open_orders),
            closed_trades=closed_count,
            wins=len(wins),
            losses=len(losses),
            win_rate=round(len(wins) / closed_count * 100, 2) if closed_count else 0.0,
            gross_pnl=round(gross, 4),
            net_pnl=round(net, 4),
            fees_total=rounded_fees,
            slippage_total=rounded_slippage,
            spread_total=rounded_spread,
            volume_impact_total=rounded_volume_impact,
            funding_total=rounded_funding,
            liquidations=liquidations,
            liquidation_fees_total=rounded_liquidation_fees,
            total_execution_cost=round(
                rounded_fees
                + rounded_slippage
                + rounded_spread
                + rounded_funding
                + rounded_liquidation_fees,
                4,
            ),
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

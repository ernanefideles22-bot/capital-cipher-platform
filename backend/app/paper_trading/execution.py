"""Deterministic execution-cost models for PAPER and backtest simulation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.backtesting.execution_data import HistoricalExecutionResolver
from app.schemas.backtest import (
    BacktestExecutionAssumptions,
    BacktestMarginAssumptions,
)
from app.schemas.common import OrderSide
from app.schemas.market import Candle
from app.schemas.paper import PaperOrder


@dataclass(frozen=True)
class SimulatedFill:
    reference_price: float
    fill_price: float
    fee_cost: float
    spread_cost: float
    slippage_cost: float
    volume_impact_cost: float
    participation_rate: float


@dataclass
class ExecutionCostLedger:
    fees: float = 0.0
    spread: float = 0.0
    slippage: float = 0.0
    volume_impact: float = 0.0
    funding: float = 0.0
    liquidation_fees: float = 0.0


class IsolatedMarginModel:
    """Deterministic isolated-margin and liquidation approximation."""

    def __init__(self, assumptions: BacktestMarginAssumptions) -> None:
        self.assumptions = assumptions

    def initial_margin(self, position_notional: float) -> float:
        return position_notional / self.assumptions.leverage

    def liquidation_price(
        self,
        *,
        side: OrderSide,
        entry_price: float,
    ) -> float:
        inverse_leverage = 1.0 / self.assumptions.leverage
        maintenance = self.assumptions.maintenance_margin_ratio
        if side == OrderSide.BUY:
            return max(
                0.0,
                entry_price * (1.0 - inverse_leverage + maintenance),
            )
        return entry_price * (
            1.0 + inverse_leverage - maintenance
        )

    def liquidation_reference(
        self,
        *,
        order: PaperOrder,
        candle: Candle,
    ) -> float | None:
        liquidation_price = order.liquidation_price
        if liquidation_price is None or liquidation_price <= 0:
            return None
        if (
            order.side == OrderSide.BUY
            and candle.low <= liquidation_price
        ):
            return min(liquidation_price, candle.open)
        if (
            order.side == OrderSide.SELL
            and candle.high >= liquidation_price
        ):
            return max(liquidation_price, candle.open)
        return None

    def liquidation_fee(self, executed_notional: float) -> float:
        return (
            executed_notional
            * self.assumptions.liquidation_fee_bps
            / 10_000
        )


class RealisticExecutionModel:
    """Conservative market fills based only on information in the candle."""

    def __init__(
        self,
        assumptions: BacktestExecutionAssumptions,
        *,
        historical_execution: HistoricalExecutionResolver | None = None,
    ) -> None:
        self.assumptions = assumptions
        self.historical_execution = historical_execution

    def open_fill(
        self,
        *,
        side: OrderSide,
        reference_price: float,
        position_notional: float,
        candle: Candle | None,
    ) -> SimulatedFill:
        return self._fill(
            action_side=side,
            reference_price=reference_price,
            position_notional=position_notional,
            quantity=None,
            candle=candle,
        )

    def close_fill(
        self,
        *,
        position_side: OrderSide,
        reference_price: float,
        quantity: float,
        candle: Candle | None,
    ) -> SimulatedFill:
        action_side = (
            OrderSide.SELL
            if position_side == OrderSide.BUY
            else OrderSide.BUY
        )
        return self._fill(
            action_side=action_side,
            reference_price=reference_price,
            position_notional=quantity * reference_price,
            quantity=quantity,
            candle=candle,
        )

    def funding_cost(
        self,
        *,
        order: PaperOrder,
        start_at: datetime,
        end_at: datetime,
    ) -> float:
        elapsed_hours = max(
            0.0,
            (end_at - start_at).total_seconds() / 3_600,
        )
        if elapsed_hours == 0:
            return 0.0
        direction = 1.0 if order.side == OrderSide.BUY else -1.0
        if self.historical_execution is not None:
            return self.historical_execution.funding_cost(
                position_notional=order.position_size,
                direction=direction,
                start_at=start_at,
                end_at=end_at,
            )
        rate = self.assumptions.funding_rate_bps_per_8h / 10_000
        return order.position_size * rate * (elapsed_hours / 8.0) * direction

    def _fill(
        self,
        *,
        action_side: OrderSide,
        reference_price: float,
        position_notional: float,
        quantity: float | None,
        candle: Candle | None,
    ) -> SimulatedFill:
        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if position_notional < 0:
            raise ValueError("position_notional must not be negative")

        quote_volume = (
            candle.volume * candle.close
            if candle is not None and candle.volume > 0
            else 0.0
        )
        participation = (
            min(1.0, position_notional / quote_volume)
            if quote_volume > 0
            else 1.0
        )
        impact_bps = (
            self.assumptions.volume_impact_bps
            * math.sqrt(participation)
        )
        half_spread_bps = self.assumptions.half_spread_bps
        if self.historical_execution is not None:
            if candle is None:
                raise ValueError(
                    "Historical execution fills require a timestamped candle"
                )
            half_spread_bps = self.historical_execution.resolve(
                candle.closed_at
            ).half_spread_bps
        adverse_bps = (
            half_spread_bps
            + self.assumptions.base_slippage_bps
            + impact_bps
        )
        direction = 1.0 if action_side == OrderSide.BUY else -1.0
        fill_price = reference_price * (
            1.0 + direction * adverse_bps / 10_000
        )
        executed_notional = (
            position_notional
            if quantity is None
            else quantity * fill_price
        )
        fee_cost = (
            executed_notional
            * self.assumptions.taker_fee_bps
            / 10_000
        )
        spread_cost = (
            position_notional
            * half_spread_bps
            / 10_000
        )
        base_slippage_cost = (
            position_notional
            * self.assumptions.base_slippage_bps
            / 10_000
        )
        volume_impact_cost = position_notional * impact_bps / 10_000
        return SimulatedFill(
            reference_price=reference_price,
            fill_price=fill_price,
            fee_cost=fee_cost,
            spread_cost=spread_cost,
            slippage_cost=base_slippage_cost + volume_impact_cost,
            volume_impact_cost=volume_impact_cost,
            participation_rate=participation,
        )

"""Venue-derived TESTNET risk accounting and portfolio sizing."""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.core.errors import SecurityError
from app.orchestrator.portfolio_consensus import PortfolioConstructionService
from app.risk.manager import RiskManager
from app.schemas.common import Exchange
from app.schemas.decisions import Decision
from app.schemas.oms import ExecutionEnvironment
from app.schemas.paper import PaperOrder
from app.schemas.portfolio_consensus import PortfolioProposal, WeightedConsensus
from app.schemas.risk import PositionExposure, RiskCheck

EquityProvider = Callable[[], Awaitable[float]]


class VenueAwareRiskManager(RiskManager):
    """Use Bybit venue state as the TESTNET risk source of truth."""

    def __init__(
        self,
        *args,
        execution_environment: ExecutionEnvironment,
        equity_cache_seconds: float = 2.0,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.execution_environment = execution_environment
        self._equity_cache_seconds = max(0.0, equity_cache_seconds)
        self._testnet_equity_provider: EquityProvider | None = None
        self._cached_equity: float | None = None
        self._cached_equity_at = 0.0
        self._venue_baseline_initialized = False
        self._testnet_day = None
        self._testnet_day_start_equity: float | None = None

    def set_testnet_equity_provider(self, provider: EquityProvider) -> None:
        if self.execution_environment != ExecutionEnvironment.TESTNET:
            raise SecurityError(
                "Venue equity provider can only be attached to TESTNET risk"
            )
        self._testnet_equity_provider = provider
        self._cached_equity = None
        self._cached_equity_at = 0.0

    async def initialize(self) -> None:
        await super().initialize()
        if self.execution_environment == ExecutionEnvironment.TESTNET:
            self.sync_positions(await self._load_bybit_testnet_positions())
            await self.effective_balance(self.initial_balance, force=True)

    async def _load_bybit_testnet_positions(self) -> list[PositionExposure]:
        if self._repository is None:
            return []
        exposures = await self._repository.load_open_position_exposures()
        orders = await self._repository.list_oms_orders(
            exchange=Exchange.BYBIT,
            environment=ExecutionEnvironment.TESTNET,
            limit=None,
        )
        managed_ids = {order.oms_order_id for order in orders}
        scoped: list[PositionExposure] = []
        for exposure in exposures:
            identity = exposure.paper_order_id
            if identity.startswith("venue:BYBIT:TESTNET:"):
                scoped.append(exposure)
                continue
            if identity.startswith("oms-reservation:"):
                order_id = identity.removeprefix("oms-reservation:")
                if order_id in managed_ids:
                    scoped.append(exposure)
                continue
            if identity.startswith("oms:"):
                order_id = identity.removeprefix("oms:")
                if order_id in managed_ids:
                    scoped.append(exposure)
        return scoped

    async def effective_balance(
        self,
        fallback_balance: float | None,
        *,
        force: bool = False,
    ) -> float:
        if self.execution_environment != ExecutionEnvironment.TESTNET:
            balance = (
                fallback_balance
                if fallback_balance is not None
                else self.initial_balance
            )
            if balance <= 0:
                raise SecurityError("Risk balance must be positive")
            return balance

        if self._testnet_equity_provider is None:
            raise SecurityError(
                "TESTNET risk has no venue equity provider configured"
            )
        now = time.monotonic()
        cache_fresh = (
            not force
            and self._cached_equity is not None
            and now - self._cached_equity_at <= self._equity_cache_seconds
        )
        if cache_fresh:
            equity = self._cached_equity
            assert equity is not None
        else:
            equity = float(await self._testnet_equity_provider())
            if not math.isfinite(equity) or equity <= 0:
                raise SecurityError(
                    "TESTNET venue equity is invalid for risk sizing"
                )
            self._cached_equity = equity
            self._cached_equity_at = now

        today = datetime.now(timezone.utc).date()
        if not self._venue_baseline_initialized:
            self.initial_balance = equity
            self._peak_equity = equity
            self._testnet_day = today
            self._testnet_day_start_equity = equity
            self._venue_baseline_initialized = True
        elif self._testnet_day != today:
            self._testnet_day = today
            self._testnet_day_start_equity = equity
            super().reset_daily()

        day_start = self._testnet_day_start_equity
        if day_start is None or day_start <= 0:
            raise SecurityError("TESTNET daily equity baseline is unavailable")
        self.state.daily_pnl_percent = (equity - day_start) / day_start * 100
        self.update_equity(equity)
        return equity

    async def refresh_positions(self) -> None:
        if self.execution_environment != ExecutionEnvironment.TESTNET:
            await super().refresh_positions()
            return
        self.sync_positions(await self._load_bybit_testnet_positions())
        await self.effective_balance(self.initial_balance, force=True)

    # The PAPER engine remains available for research APIs even in a TESTNET
    # process. Its historical book must never alter TESTNET PnL or exposure.
    def restore_realized_history(
        self,
        orders: list[PaperOrder],
        initial_balance: float,
    ) -> None:
        if self.execution_environment == ExecutionEnvironment.TESTNET:
            return
        super().restore_realized_history(orders, initial_balance)

    def register_trade_result(self, pnl: float) -> None:
        if self.execution_environment == ExecutionEnvironment.TESTNET:
            return
        super().register_trade_result(pnl)

    def register_position(self, order: PaperOrder) -> None:
        if self.execution_environment == ExecutionEnvironment.TESTNET:
            return
        super().register_position(order)

    def unregister_position(self, paper_order_id: str) -> None:
        if self.execution_environment == ExecutionEnvironment.TESTNET:
            return
        super().unregister_position(paper_order_id)

    async def check(
        self,
        decision: Decision,
        *,
        entry_price: float,
        atr: float | None = None,
        data_quality_score: int = 100,
        market_data_delay_ms: int = 0,
        balance: float | None = None,
        leverage: float | None = None,
        idempotency_key: str | None = None,
        risk_per_trade_percent_override: float | None = None,
        min_risk_reward_override: float | None = None,
        max_open_positions_override: int | None = None,
        max_strategy_exposure_percent_override: float | None = None,
        max_portfolio_var_percent_override: float | None = None,
        max_notional_override: float | None = None,
    ) -> RiskCheck:
        venue_balance = await self.effective_balance(balance)
        return await super().check(
            decision,
            entry_price=entry_price,
            atr=atr,
            data_quality_score=data_quality_score,
            market_data_delay_ms=market_data_delay_ms,
            balance=venue_balance,
            leverage=leverage,
            idempotency_key=idempotency_key,
            risk_per_trade_percent_override=risk_per_trade_percent_override,
            min_risk_reward_override=min_risk_reward_override,
            max_open_positions_override=max_open_positions_override,
            max_strategy_exposure_percent_override=(
                max_strategy_exposure_percent_override
            ),
            max_portfolio_var_percent_override=(
                max_portfolio_var_percent_override
            ),
            max_notional_override=max_notional_override,
        )


class VenueAwarePortfolioConstructionService(PortfolioConstructionService):
    """Use the same venue equity source before central risk evaluation."""

    async def propose(
        self,
        *,
        decision: Decision,
        consensus: WeightedConsensus,
        balance: float,
    ) -> PortfolioProposal:
        effective_balance = await self._risk.effective_balance(balance)
        return await super().propose(
            decision=decision,
            consensus=consensus,
            balance=effective_balance,
        )

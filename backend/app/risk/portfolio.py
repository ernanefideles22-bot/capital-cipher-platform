"""Deterministic portfolio exposure and Value-at-Risk calculations."""

from __future__ import annotations

from math import ceil

from app.market_data.store import CandleStore
from app.schemas.common import OrderSide
from app.schemas.risk import (
    PortfolioRiskSnapshot,
    PositionExposure,
    RiskLimits,
    VaRResult,
)

NORMAL_Z_99 = 2.326347874


def signed_notional(position: PositionExposure) -> float:
    return position.notional if position.side == OrderSide.BUY else -position.notional


def exposure_snapshot(
    positions: list[PositionExposure],
    proposed: PositionExposure,
    *,
    balance: float,
) -> PortfolioRiskSnapshot:
    current_gross = sum(position.notional for position in positions)
    current_net = sum(signed_notional(position) for position in positions)
    proposed_signed = signed_notional(proposed)
    symbol_current = sum(
        position.notional
        for position in positions
        if position.symbol == proposed.symbol
    )
    strategy_current = sum(
        position.notional
        for position in positions
        if position.strategy == proposed.strategy
    )
    resulting_gross = current_gross + proposed.notional
    symbol_resulting = symbol_current + proposed.notional
    concentration = (
        symbol_resulting / resulting_gross * 100 if resulting_gross else 0.0
    )
    return PortfolioRiskSnapshot(
        balance=balance,
        position_count=len(positions),
        gross_exposure=round(current_gross, 8),
        net_exposure=round(current_net, 8),
        proposed_notional=round(proposed.notional, 8),
        resulting_gross_exposure=round(resulting_gross, 8),
        resulting_net_exposure=round(current_net + proposed_signed, 8),
        symbol_exposure=round(symbol_resulting, 8),
        strategy_exposure=round(strategy_current + proposed.notional, 8),
        symbol_concentration_percent=round(concentration, 8),
    )


def remaining_notional_capacity(
    positions: list[PositionExposure],
    proposed: PositionExposure,
    *,
    balance: float,
    limits: RiskLimits,
    strategy_exposure_limit_percent: float,
) -> tuple[float, list[str]]:
    """Return the maximum notional still allowed by all absolute limits."""

    gross = sum(position.notional for position in positions)
    net = sum(signed_notional(position) for position in positions)
    symbol = sum(
        position.notional
        for position in positions
        if position.symbol == proposed.symbol
    )
    strategy = sum(
        position.notional
        for position in positions
        if position.strategy == proposed.strategy
    )
    direction = 1.0 if proposed.side == OrderSide.BUY else -1.0
    gross_capacity = balance * limits.max_gross_exposure_percent / 100 - gross
    symbol_capacity = balance * limits.max_symbol_exposure_percent / 100 - symbol
    strategy_capacity = balance * strategy_exposure_limit_percent / 100 - strategy
    position_capacity = balance * limits.max_single_position_percent / 100
    if direction > 0:
        net_capacity = balance * limits.max_net_exposure_percent / 100 - net
    else:
        net_capacity = balance * limits.max_net_exposure_percent / 100 + net
    capacities = {
        "GROSS_EXPOSURE_LIMIT": gross_capacity,
        "NET_EXPOSURE_LIMIT": net_capacity,
        "SYMBOL_EXPOSURE_LIMIT": symbol_capacity,
        "STRATEGY_EXPOSURE_LIMIT": strategy_capacity,
        "SINGLE_POSITION_LIMIT": position_capacity,
    }
    maximum = min(proposed.notional, *capacities.values())
    binding = [
        name
        for name, capacity in capacities.items()
        if capacity <= maximum + 1e-9
    ]
    return max(0.0, maximum), sorted(binding)


def _returns_for(
    candle_store: CandleStore,
    position: PositionExposure,
    *,
    lookback: int,
) -> list[float]:
    candles = candle_store.get(
        "BINANCE",
        position.symbol,
        position.timeframe,
        limit=lookback + 1,
    )
    returns: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        if previous.close > 0:
            returns.append(current.close / previous.close - 1)
    return returns[-lookback:]


def portfolio_var(
    positions: list[PositionExposure],
    *,
    balance: float,
    limits: RiskLimits,
    candle_store: CandleStore | None,
) -> VaRResult:
    """Historical VaR when data is sufficient, conservative proxy otherwise."""

    if not positions:
        return VaRResult(
            method="no-exposure",
            confidence=limits.var_confidence,
            observations=0,
            value_at_risk=0,
            value_at_risk_percent=0,
            expected_shortfall=0,
        )

    series: list[list[float]] = []
    if candle_store is not None:
        series = [
            _returns_for(candle_store, position, lookback=limits.var_lookback)
            for position in positions
        ]
    observations = min((len(item) for item in series), default=0)
    if observations >= limits.var_min_observations:
        losses: list[float] = []
        for index in range(-observations, 0):
            pnl = sum(
                signed_notional(position) * returns[index]
                for position, returns in zip(positions, series)
            )
            losses.append(max(0.0, -pnl))
        ordered = sorted(losses)
        quantile_index = min(
            len(ordered) - 1,
            max(0, ceil(limits.var_confidence * len(ordered)) - 1),
        )
        value = ordered[quantile_index]
        tail = [loss for loss in ordered if loss >= value]
        expected_shortfall = sum(tail) / len(tail) if tail else value
        return VaRResult(
            method="historical-v1",
            confidence=limits.var_confidence,
            observations=observations,
            value_at_risk=round(value, 8),
            value_at_risk_percent=round(value / balance * 100, 8),
            expected_shortfall=round(expected_shortfall, 8),
        )

    gross = sum(position.notional for position in positions)
    value = (
        gross
        * (limits.fallback_volatility_percent / 100)
        * NORMAL_Z_99
    )
    return VaRResult(
        method="proxy-v1",
        confidence=limits.var_confidence,
        observations=observations,
        value_at_risk=round(value, 8),
        value_at_risk_percent=round(value / balance * 100, 8),
        expected_shortfall=round(value, 8),
        warnings=["VAR_PROXY_USED", "INSUFFICIENT_RETURN_HISTORY"],
    )

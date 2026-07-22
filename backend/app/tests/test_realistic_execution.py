"""Deterministic spread, slippage, impact, fee, and funding tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.backtesting.engine import BacktestingEngine
from app.core.config import Settings
from app.paper_trading.engine import PaperTradingEngine
from app.paper_trading.execution import RealisticExecutionModel
from app.schemas.backtest import (
    BacktestExecutionAssumptions,
    BacktestRequest,
)
from app.schemas.common import OrderSide
from app.schemas.paper import PaperOrder
from app.schemas.strategy import StrategyConfig
from app.strategy.engine import StrategyEngine
from app.tests.conftest import make_candle, make_decision, make_series


def test_fill_is_adverse_and_volume_impact_is_deterministic():
    assumptions = BacktestExecutionAssumptions(
        taker_fee_bps=10,
        half_spread_bps=2,
        base_slippage_bps=3,
        volume_impact_bps=20,
    )
    model = RealisticExecutionModel(assumptions)
    candle = make_candle(close=100, volume=1_000)

    buy = model.open_fill(
        side=OrderSide.BUY,
        reference_price=100,
        position_notional=1_000,
        candle=candle,
    )
    sell = model.open_fill(
        side=OrderSide.SELL,
        reference_price=100,
        position_notional=1_000,
        candle=candle,
    )
    long_exit = model.close_fill(
        position_side=OrderSide.BUY,
        reference_price=100,
        quantity=10,
        candle=candle,
    )

    assert buy.participation_rate == pytest.approx(0.01)
    assert buy.fill_price == pytest.approx(100.07)
    assert sell.fill_price == pytest.approx(99.93)
    assert long_exit.fill_price == pytest.approx(99.93)
    assert buy.fee_cost == pytest.approx(1.0)
    assert buy.spread_cost == pytest.approx(0.2)
    assert buy.volume_impact_cost == pytest.approx(0.2)
    assert buy.slippage_cost == pytest.approx(0.5)


def test_zero_volume_uses_full_conservative_impact():
    model = RealisticExecutionModel(
        BacktestExecutionAssumptions(
            taker_fee_bps=0,
            half_spread_bps=0,
            base_slippage_bps=0,
            volume_impact_bps=25,
        )
    )
    fill = model.open_fill(
        side=OrderSide.BUY,
        reference_price=100,
        position_notional=1_000,
        candle=make_candle(close=100, volume=0),
    )

    assert fill.participation_rate == 1.0
    assert fill.fill_price == pytest.approx(100.25)


def test_signed_funding_charges_longs_and_credits_shorts():
    model = RealisticExecutionModel(
        BacktestExecutionAssumptions(funding_rate_bps_per_8h=1)
    )
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    common = {
        "decision_id": "decision",
        "risk_check_id": "risk-check",
        "correlation_id": "correlation",
        "exchange": "BINANCE",
        "symbol": "BTCUSDT",
        "position_size": 8_000,
        "entry_price": 100,
    }
    long = PaperOrder(side="BUY", **common)
    short = PaperOrder(side="SELL", **common)

    assert model.funding_cost(
        order=long,
        start_at=start,
        end_at=start + timedelta(hours=8),
    ) == pytest.approx(0.8)
    assert model.funding_cost(
        order=short,
        start_at=start,
        end_at=start + timedelta(hours=8),
    ) == pytest.approx(-0.8)


async def test_paper_round_trip_accounts_for_every_execution_cost(
    paper_state_machine,
    audit_service,
    risk_manager,
):
    started = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assumptions = BacktestExecutionAssumptions(
        taker_fee_bps=8,
        half_spread_bps=1,
        base_slippage_bps=2,
        volume_impact_bps=10,
        funding_rate_bps_per_8h=1,
    )
    engine = PaperTradingEngine(
        audit_service,
        risk_manager,
        execution_model=RealisticExecutionModel(assumptions),
        started_at=started,
    )
    decision = make_decision()
    check = await risk_manager.check(
        decision,
        entry_price=100,
        atr=1,
    )
    entry_candle = make_candle(
        close=100,
        volume=100,
        closed_at=started,
    )
    order = await engine.create_order(
        decision,
        check,
        current_price=100,
        market_candle=entry_candle,
        occurred_at=started,
    )
    exit_at = started + timedelta(hours=8)
    exit_candle = make_candle(
        close=100,
        volume=100,
        closed_at=exit_at,
    )
    closed = await engine.close_order(
        order.paper_order_id,
        100,
        "TEST_END",
        market_candle=exit_candle,
        occurred_at=exit_at,
    )
    performance = engine.performance()

    assert order.entry_price > 100
    assert closed.exit_price < 100
    assert closed.pnl < 0
    assert performance.fees_total > 0
    assert performance.spread_total > 0
    assert performance.slippage_total > 0
    assert performance.volume_impact_total > 0
    assert performance.funding_total > 0
    expected_pnl = (
        (closed.exit_price - order.entry_price)
        * (order.position_size / order.entry_price)
        - closed.fees_estimated
        - performance.funding_total
    )
    assert closed.pnl == pytest.approx(round(expected_pnl, 4))
    assert performance.total_execution_cost == pytest.approx(
        round(
            performance.fees_total
            + performance.spread_total
            + performance.slippage_total
            + performance.funding_total,
            4,
        )
    )
    assert engine.equity_curve[0].timestamp == started.isoformat()
    assert engine.equity_curve[-1].timestamp == exit_at.isoformat()


async def test_backtest_report_records_assumptions_and_costs_reduce_pnl():
    strategy = StrategyEngine(
        [
            StrategyConfig(
                strategy_id="EXECUTION_TEST",
                symbols=["BTCUSDT"],
                timeframe="15m",
                minimum_confidence=60,
            )
        ]
    )
    candles = make_series([100 * (1.001 ** index) for index in range(250)])
    cheap_assumptions = BacktestExecutionAssumptions(
        taker_fee_bps=0,
        half_spread_bps=0,
        base_slippage_bps=0,
        volume_impact_bps=0,
        funding_rate_bps_per_8h=0,
    )
    expensive_assumptions = BacktestExecutionAssumptions(
        taker_fee_bps=8,
        half_spread_bps=2,
        base_slippage_bps=3,
        volume_impact_bps=20,
        funding_rate_bps_per_8h=1,
    )

    cheap = await BacktestingEngine(strategy_engine=strategy).run(
        BacktestRequest(execution=cheap_assumptions),
        candles,
    )
    expensive = await BacktestingEngine(strategy_engine=strategy).run(
        BacktestRequest(execution=expensive_assumptions),
        candles,
    )

    assert cheap.total_trades > 0
    assert expensive.total_trades > 0
    assert expensive.net_pnl < cheap.net_pnl
    assert expensive.execution_assumptions == expensive_assumptions
    assert expensive.fees > 0
    assert expensive.spread > 0
    assert expensive.slippage > 0
    assert expensive.volume_impact > 0
    assert expensive.funding > 0
    assert expensive.total_execution_cost == pytest.approx(
        round(
            expensive.fees
            + expensive.spread
            + expensive.slippage
            + expensive.funding,
            4,
        )
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("taker_fee_bps", -1),
        ("half_spread_bps", -1),
        ("base_slippage_bps", -1),
        ("volume_impact_bps", -1),
        ("funding_rate_bps_per_8h", 1_001),
    ],
)
def test_execution_assumptions_fail_closed(field: str, value: float):
    with pytest.raises(ValidationError):
        BacktestExecutionAssumptions(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("FEE_RATE_PERCENT", -0.01),
        ("FEE_RATE_PERCENT", 10.01),
        ("SLIPPAGE_RATE_PERCENT", -0.01),
        ("SLIPPAGE_RATE_PERCENT", 10.01),
        ("BACKTEST_HALF_SPREAD_BPS", -1),
        ("BACKTEST_VOLUME_IMPACT_BPS", -1),
        ("BACKTEST_FUNDING_RATE_BPS_PER_8H", 1_001),
    ],
)
def test_execution_configuration_fails_closed(field: str, value: float):
    with pytest.raises(ValidationError):
        Settings(**{field: value})

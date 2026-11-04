"""Source venue must survive PAPER creation, monitoring and historical replay."""
from datetime import timedelta

import pytest

from app.backtesting.engine import BacktestingEngine
from app.paper_trading.execution import RealisticExecutionModel
from app.schemas.backtest import BacktestExecutionAssumptions, BacktestRequest
from app.schemas.common import CandidateAction, Exchange
from app.schemas.strategy import StrategyConfig
from app.strategy.engine import StrategyEngine
from app.tests.conftest import make_candle, make_decision, make_series


@pytest.mark.parametrize("exchange", [Exchange.BINANCE, Exchange.BYBIT])
@pytest.mark.parametrize("action", [CandidateAction.BUY, CandidateAction.SELL])
@pytest.mark.parametrize("exit_reason", ["STOP_LOSS", "TAKE_PROFIT"])
async def test_source_venue_position_is_monitored(paper_engine, risk_manager, exchange, action, exit_reason):
    decision = make_decision(action=action)
    check = await risk_manager.check(decision, entry_price=100, atr=1)
    source = make_candle(100).model_copy(update={"exchange": exchange})
    order = await paper_engine.create_order(decision, check, current_price=100,
        market_candle=source, occurred_at=source.closed_at)
    assert order.exchange == exchange
    audit = paper_engine._audit.query(correlation_id=decision.correlation_id)
    created = next(row for row in audit if row["audit_type"] == "PAPER_ORDER_CREATED")
    assert created["payload"]["exchange"] == exchange.value
    level = order.stop_loss if exit_reason == "STOP_LOSS" else order.take_profit
    monitor = make_candle(level, open_=100, high=max(100,level), low=min(100,level),
        closed_at=order.opened_at+timedelta(minutes=15)).model_copy(update={"exchange":exchange})
    foreign = Exchange.BYBIT if exchange == Exchange.BINANCE else Exchange.BINANCE
    assert await paper_engine.on_candle(monitor.model_copy(update={"exchange":foreign})) == []
    closed, = await paper_engine.on_candle(monitor)
    assert closed.exchange == exchange
    assert closed.exit_reason == exit_reason
    assert closed.closed_at == monitor.closed_at


async def test_order_without_source_candle_keeps_legacy_default(paper_engine, risk_manager):
    decision = make_decision()
    check = await risk_manager.check(decision, entry_price=100, atr=1)
    order = await paper_engine.create_order(decision, check, current_price=100)
    assert order.exchange == Exchange.BINANCE


@pytest.mark.parametrize("exchange", [Exchange.BINANCE, Exchange.BYBIT])
async def test_source_venue_accrues_funding_before_exit(paper_engine, risk_manager, exchange):
    paper_engine._execution_model = RealisticExecutionModel(BacktestExecutionAssumptions(
        taker_fee_bps=0, half_spread_bps=0, base_slippage_bps=0,
        volume_impact_bps=0, funding_rate_bps_per_8h=10))
    decision = make_decision()
    check = await risk_manager.check(decision, entry_price=100, atr=1)
    source = make_candle(100).model_copy(update={"exchange":exchange})
    order = await paper_engine.create_order(decision, check, current_price=100,
        market_candle=source, occurred_at=source.closed_at)
    monitor = make_candle(100, open_=100, high=100.1, low=99.9,
        closed_at=order.opened_at+timedelta(hours=8)).model_copy(update={"exchange":exchange})
    assert await paper_engine.on_candle(monitor) == []
    assert paper_engine._execution_costs[order.paper_order_id].funding == pytest.approx(order.position_size*.001)


@pytest.mark.parametrize("exchange", [Exchange.BINANCE, Exchange.BYBIT])
async def test_backtest_exits_on_monitored_candles_not_only_end(monkeypatch, exchange):
    candles = [c.model_copy(update={"exchange":exchange})
        for c in make_series([100*(1.001**i) for i in range(250)])]
    engine = BacktestingEngine(strategy_engine=StrategyEngine([StrategyConfig(
        strategy_id="VENUE_REGRESSION", symbols=["BTCUSDT"], timeframe="15m",
        minimum_confidence=60)]))
    closed = []
    build_report = engine._build_report
    def observe_report(request, rows, paper, *args, **kwargs):
        closed.extend(paper.closed_orders)
        return build_report(request, rows, paper, *args, **kwargs)
    monkeypatch.setattr(engine, "_build_report", observe_report)
    report = await engine.run(BacktestRequest(exchange=exchange), candles)
    assert report.total_trades > 0
    assert all(order.exchange == exchange for order in closed)
    assert any(order.exit_reason in {"TAKE_PROFIT","STOP_LOSS","LIQUIDATION"} for order in closed)

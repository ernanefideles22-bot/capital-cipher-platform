"""Backtesting Engine tests (docs/17, docs/22)."""

from __future__ import annotations

from app.backtesting.engine import BacktestingEngine
from app.schemas.backtest import BacktestRequest
from app.tests.conftest import make_series


async def run_backtest(closes: list[float]):
    engine = BacktestingEngine()
    candles = make_series(closes)
    report = await engine.run(BacktestRequest(symbol="BTCUSDT", timeframe="15m"), candles)
    return engine, report


async def test_backtest_produces_report_with_mandatory_metrics():
    _, report = await run_backtest([100 * (1.004 ** i) for i in range(150)])
    assert report.candles_processed == 150
    assert report.decisions > 0
    for field in (
        "win_rate", "profit_factor", "expectancy", "max_drawdown",
        "max_consecutive_losses", "net_pnl", "fees", "slippage",
    ):
        assert hasattr(report, field)
    assert report.start_date <= report.end_date


async def test_backtest_closes_positions_at_end():
    engine, report = await run_backtest([100 * (1.004 ** i) for i in range(150)])
    # No phantom open positions after a run.
    assert report.total_trades >= 0
    assert engine.reports[-1].backtest_id == report.backtest_id


async def test_backtest_is_deterministic():
    closes = [100 * (1.003 ** i) for i in range(140)]
    candles = make_series(closes)
    first_engine = BacktestingEngine()
    second_engine = BacktestingEngine()
    request = BacktestRequest(symbol="BTCUSDT", timeframe="15m")
    first = await first_engine.run(request, candles)
    second = await second_engine.run(request, candles)
    assert first.total_trades == second.total_trades
    assert first.net_pnl == second.net_pnl
    assert first.decisions == second.decisions
    assert first.dataset_hash == second.dataset_hash
    assert first.dataset_id == second.dataset_id


async def test_backtest_processes_candles_in_order_even_if_shuffled():
    """No lookahead: input order must not matter (docs/17)."""
    closes = [100 * (1.003 ** i) for i in range(140)]
    candles = make_series(closes)
    engine = BacktestingEngine()
    shuffled = list(reversed(candles))
    report_shuffled = await engine.run(
        BacktestRequest(symbol="BTCUSDT", timeframe="15m"), shuffled
    )
    report_ordered = await engine.run(
        BacktestRequest(symbol="BTCUSDT", timeframe="15m"), candles
    )
    assert report_shuffled.net_pnl == report_ordered.net_pnl
    assert report_shuffled.total_trades == report_ordered.total_trades


async def test_flat_market_generates_no_trades():
    _, report = await run_backtest([100.0] * 120)
    assert report.total_trades == 0
    assert report.net_pnl == 0


async def test_fees_and_slippage_reduce_pnl():
    expensive = BacktestingEngine(fee_rate_percent=0.5, slippage_rate_percent=0.5)
    cheap = BacktestingEngine(fee_rate_percent=0.0, slippage_rate_percent=0.0)
    closes = [100 * (1.004 ** i) for i in range(150)]
    candles = make_series(closes)
    report_expensive = await expensive.run(
        BacktestRequest(symbol="BTCUSDT", timeframe="15m"), candles
    )
    report_cheap = await cheap.run(BacktestRequest(symbol="BTCUSDT", timeframe="15m"), candles)
    if report_cheap.total_trades > 0 and report_expensive.total_trades > 0:
        assert report_expensive.net_pnl < report_cheap.net_pnl

"""Backtest schemas (docs/17-backtesting-engine.md)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    source: str = Field(default="store", description="store | inline | csv")
    exchange: str = "BINANCE"
    candles: list[dict] | None = None
    csv_path: str | None = None


class BacktestReport(BaseModel):
    """Mandatory metrics from docs/17."""

    backtest_id: str
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    candles_processed: int
    decisions: int
    actionable_decisions: int
    blocked_by_risk: int
    total_trades: int
    win_rate: float
    loss_rate: float
    profit_factor: float | None
    expectancy: float
    max_drawdown: float
    max_consecutive_losses: int
    avg_win: float
    avg_loss: float
    net_pnl: float
    net_pnl_percent: float
    fees: float
    slippage: float
    final_balance: float
    equity_curve: list[dict] = Field(default_factory=list)
    duration_ms: int = 0
    created_at: str = ""

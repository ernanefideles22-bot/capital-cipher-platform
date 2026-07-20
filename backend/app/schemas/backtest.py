"""Backtest schemas (docs/17-backtesting-engine.md)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.events import CONTRACT_VERSION


class BacktestExecutionAssumptions(BaseModel):
    """Versioned, explicit cost assumptions for deterministic simulation."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    model_version: Literal["realistic-v1"] = "realistic-v1"
    taker_fee_bps: float = Field(default=8.0, ge=0, le=1_000)
    half_spread_bps: float = Field(default=1.0, ge=0, le=1_000)
    base_slippage_bps: float = Field(default=2.0, ge=0, le=1_000)
    volume_impact_bps: float = Field(default=10.0, ge=0, le=10_000)
    funding_rate_bps_per_8h: float = Field(
        default=0.0,
        ge=-1_000,
        le=1_000,
    )


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"
    source: str = Field(default="store", description="store | inline | csv")
    exchange: str = "BINANCE"
    candles: list[dict] | None = None
    csv_path: str | None = None
    execution: BacktestExecutionAssumptions | None = None


class BacktestReport(BaseModel):
    """Mandatory metrics from docs/17."""

    backtest_id: str
    dataset_id: str
    dataset_hash: str
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
    spread: float = 0.0
    volume_impact: float = 0.0
    funding: float = 0.0
    total_execution_cost: float = 0.0
    execution_assumptions: BacktestExecutionAssumptions = Field(
        default_factory=BacktestExecutionAssumptions
    )
    final_balance: float
    equity_curve: list[dict] = Field(default_factory=list)
    duration_ms: int = 0
    created_at: str = ""

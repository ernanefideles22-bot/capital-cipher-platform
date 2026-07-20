"""Backtest schemas (docs/17-backtesting-engine.md)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

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
    source: Literal["store", "inline", "csv"] = "store"
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


class WalkForwardProtocol(BaseModel):
    """Versioned temporal split rules for pre-registered candidates."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    protocol_version: Literal["walk-forward-v1"] = "walk-forward-v1"
    selection_mode: Literal["pre-registered"] = "pre-registered"
    train_candles: int = Field(default=250, ge=1, le=1_000_000)
    validation_candles: int = Field(default=100, ge=1, le=1_000_000)
    test_candles: int = Field(default=100, ge=1, le=1_000_000)
    embargo_candles: int = Field(default=1, ge=0, le=100_000)
    step_candles: int | None = Field(default=None, ge=1, le=1_000_000)
    anchored_train: bool = False
    max_folds: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_non_overlapping_test_windows(self) -> "WalkForwardProtocol":
        if (
            self.step_candles is not None
            and self.step_candles < self.test_candles
        ):
            raise ValueError(
                "step_candles must be at least test_candles so test windows "
                "cannot overlap"
            )
        return self

    @property
    def resolved_step_candles(self) -> int:
        return self.step_candles or self.test_candles


class WalkForwardRequest(BaseModel):
    """A pre-registered strategy evaluated without automatic model selection."""

    backtest: BacktestRequest = Field(default_factory=BacktestRequest)
    candidate_version: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    protocol: WalkForwardProtocol = Field(default_factory=WalkForwardProtocol)


class WalkForwardSegment(BaseModel):
    role: Literal["TRAIN", "VALIDATION", "TEST"]
    start_index: int = Field(ge=0)
    end_index_exclusive: int = Field(gt=0)
    candles: int = Field(gt=0)
    start_at: AwareDatetime
    end_at: AwareDatetime
    dataset_id: str
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WalkForwardBacktestSummary(BaseModel):
    dataset_id: str
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candles_processed: int = Field(gt=0)
    total_trades: int = Field(ge=0)
    net_pnl: float
    net_pnl_percent: float
    expectancy: float
    max_drawdown: float = Field(ge=0)
    fees: float = Field(ge=0)
    slippage: float = Field(ge=0)
    funding: float
    total_execution_cost: float


class WalkForwardFoldReport(BaseModel):
    fold_id: str = Field(pattern=r"^walk-forward-fold:v1:[a-f0-9]{64}$")
    fold_index: int = Field(ge=0)
    train: WalkForwardSegment
    validation: WalkForwardSegment
    test: WalkForwardSegment
    validation_result: WalkForwardBacktestSummary
    test_result: WalkForwardBacktestSummary


class WalkForwardAggregate(BaseModel):
    folds: int = Field(ge=0)
    total_trades: int = Field(ge=0)
    profitable_folds: int = Field(ge=0)
    profitable_fold_ratio: float = Field(ge=0, le=1)
    mean_net_pnl_percent: float
    median_net_pnl_percent: float
    worst_max_drawdown: float = Field(ge=0)
    mean_expectancy: float


class WalkForwardReport(BaseModel):
    """Compact research-only result with validation and test kept separate."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    experiment_id: str = Field(pattern=r"^walk-forward:v1:[a-f0-9]{64}$")
    dataset_id: str
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbol: str
    timeframe: str
    candidate_version: str
    protocol: WalkForwardProtocol
    resolved_step_candles: int = Field(gt=0)
    execution_assumptions: BacktestExecutionAssumptions
    simulation_context: dict[str, Any]
    simulation_context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    folds: list[WalkForwardFoldReport]
    validation_aggregate: WalkForwardAggregate
    test_aggregate: WalkForwardAggregate
    promotion_status: Literal["RESEARCH_ONLY"] = "RESEARCH_ONLY"
    duration_ms: int = Field(default=0, ge=0)
    created_at: AwareDatetime

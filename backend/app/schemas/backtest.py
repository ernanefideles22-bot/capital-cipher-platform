"""Backtest schemas (docs/17-backtesting-engine.md)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.common import Exchange
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


class HistoricalExecutionObservation(BaseModel):
    """Observed spread and funding values available at a historical instant."""

    observed_at: AwareDatetime
    half_spread_bps: float = Field(ge=0, le=1_000)
    funding_rate_bps_per_8h: float = Field(ge=-1_000, le=1_000)
    source_record_id: str = Field(min_length=1, max_length=256)


class HistoricalExecutionDataset(BaseModel):
    """Versioned as-of data used instead of constant spread/funding values."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    dataset_version: Literal["historical-execution-v1"] = (
        "historical-execution-v1"
    )
    source: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]+$",
    )
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    max_age_seconds: int = Field(default=28_800, ge=1, le=604_800)
    observations: list[HistoricalExecutionObservation] = Field(
        min_length=1,
        max_length=1_000_000,
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_dataset_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_strictly_ordered_observations(
        self,
    ) -> "HistoricalExecutionDataset":
        timestamps = [item.observed_at for item in self.observations]
        if timestamps != sorted(timestamps):
            raise ValueError(
                "historical execution observations must be ordered"
            )
        if len(set(timestamps)) != len(timestamps):
            raise ValueError(
                "historical execution observations contain duplicate times"
            )
        record_ids = [
            item.source_record_id for item in self.observations
        ]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError(
                "historical execution observations contain duplicate "
                "source_record_id values"
            )
        return self


class HistoricalExecutionDatasetManifest(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    dataset_version: Literal["historical-execution-v1"] = (
        "historical-execution-v1"
    )
    dataset_id: str = Field(
        pattern=r"^historical-execution:v1:[a-f0-9]{64}$"
    )
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str
    exchange: Exchange
    symbol: str
    row_count: int = Field(gt=0)
    start_at: AwareDatetime
    end_at: AwareDatetime
    max_age_seconds: int = Field(gt=0)


class BacktestMarginAssumptions(BaseModel):
    """Conservative isolated-margin assumptions used only in simulation."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    model_version: Literal["isolated-margin-v1"] = "isolated-margin-v1"
    leverage: float = Field(default=1.0, ge=1.0, le=125.0)
    maintenance_margin_ratio: float = Field(
        default=0.005,
        ge=0,
        lt=0.5,
    )
    liquidation_fee_bps: float = Field(default=10.0, ge=0, le=1_000)


class BacktestRequest(BaseModel):
    symbol: str = Field(
        default="BTCUSDT",
        pattern=r"^[A-Z0-9._-]{2,32}$",
    )
    timeframe: str = Field(default="15m", pattern=r"^[1-9][0-9]*[mhdw]$")
    source: Literal["store", "inline", "csv"] = "store"
    exchange: Exchange = Exchange.BINANCE
    candles: list[dict] | None = None
    csv_path: str | None = None
    execution: BacktestExecutionAssumptions | None = None
    historical_execution: HistoricalExecutionDataset | None = None
    margin: BacktestMarginAssumptions = Field(
        default_factory=BacktestMarginAssumptions
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_execution_dataset_series(self) -> "BacktestRequest":
        dataset = self.historical_execution
        if dataset is not None and (
            dataset.exchange != self.exchange
            or dataset.symbol != self.symbol
        ):
            raise ValueError(
                "historical execution dataset does not match the requested "
                "exchange and symbol"
            )
        return self


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
    liquidations: int = 0
    liquidation_fees: float = 0.0
    total_execution_cost: float = 0.0
    execution_assumptions: BacktestExecutionAssumptions = Field(
        default_factory=BacktestExecutionAssumptions
    )
    historical_execution_manifest: (
        HistoricalExecutionDatasetManifest | None
    ) = None
    margin_assumptions: BacktestMarginAssumptions = Field(
        default_factory=BacktestMarginAssumptions
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


class WalkForwardResearchPlan(BaseModel):
    """Pre-registered candidate budget and family-wise error control."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    plan_version: Literal["research-plan-v1"] = "research-plan-v1"
    research_program_id: str = Field(
        default="default-research-program",
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    candidate_index: int = Field(default=1, ge=1, le=10_000)
    candidate_budget: int = Field(default=1, ge=1, le=10_000)
    familywise_alpha: float = Field(default=0.05, gt=0, le=0.25)
    multiple_testing_correction: Literal["bonferroni"] = "bonferroni"

    @model_validator(mode="after")
    def validate_candidate_budget(self) -> "WalkForwardResearchPlan":
        if self.candidate_index > self.candidate_budget:
            raise ValueError(
                "candidate_index must not exceed candidate_budget"
            )
        return self


class WalkForwardAcceptanceCriteria(BaseModel):
    """Versioned gates fixed before validation and test are evaluated."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    gate_version: Literal["walk-forward-gate-v1"] = (
        "walk-forward-gate-v1"
    )
    minimum_folds: int = Field(default=3, ge=1, le=100)
    minimum_trades: int = Field(default=30, ge=1, le=1_000_000)
    minimum_profitable_fold_ratio: float = Field(
        default=0.6,
        ge=0,
        le=1,
    )
    minimum_median_net_pnl_percent: float = Field(default=0.0)
    minimum_mean_expectancy: float = Field(default=0.0)
    maximum_worst_drawdown_percent: float = Field(
        default=10.0,
        gt=0,
        le=100,
    )
    require_zero_liquidations: bool = True


class WalkForwardRequest(BaseModel):
    """A candidate and its evaluation rules, fixed before replay begins."""

    backtest: BacktestRequest = Field(default_factory=BacktestRequest)
    candidate_version: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    protocol: WalkForwardProtocol = Field(default_factory=WalkForwardProtocol)
    research_plan: WalkForwardResearchPlan = Field(
        default_factory=WalkForwardResearchPlan
    )
    acceptance: WalkForwardAcceptanceCriteria = Field(
        default_factory=WalkForwardAcceptanceCriteria
    )


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
    liquidations: int = Field(default=0, ge=0)
    liquidation_fees: float = Field(default=0.0, ge=0)
    total_execution_cost: float


class WalkForwardFittedCandidate(BaseModel):
    """Immutable train-only fitting result for one fold."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    fitter_version: Literal["frozen-strategy-fitter-v1"] = (
        "frozen-strategy-fitter-v1"
    )
    candidate_version: str
    fold_index: int = Field(ge=0)
    train_dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    training_rows: int = Field(gt=0)
    parameters_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diagnostics: dict[str, float]
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WalkForwardFoldReport(BaseModel):
    fold_id: str = Field(
        pattern=r"^walk-forward-fold:v[12]:[a-f0-9]{64}$"
    )
    fold_index: int = Field(ge=0)
    train: WalkForwardSegment
    validation: WalkForwardSegment
    test: WalkForwardSegment
    fitted_candidate: WalkForwardFittedCandidate | None = None
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
    total_liquidations: int = Field(default=0, ge=0)


class WalkForwardGateResult(BaseModel):
    """Explainable phase result; never an authorization for live execution."""

    phase: Literal["VALIDATION", "TEST"]
    passed: bool
    reasons: list[str]
    raw_sign_test_p_value: float = Field(ge=0, le=1)
    adjusted_p_value: float = Field(ge=0, le=1)
    familywise_alpha: float = Field(gt=0, le=0.25)
    observed: dict[str, Any]


class WalkForwardReport(BaseModel):
    """Compact research-only result with validation and test kept separate."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    report_version: Literal["walk-forward-report-v2"] | None = None
    experiment_id: str = Field(
        pattern=r"^walk-forward:v[12]:[a-f0-9]{64}$"
    )
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_id: str
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbol: str
    timeframe: str
    candidate_version: str
    protocol: WalkForwardProtocol
    resolved_step_candles: int = Field(gt=0)
    execution_assumptions: BacktestExecutionAssumptions
    historical_execution_manifest: (
        HistoricalExecutionDatasetManifest | None
    ) = None
    margin_assumptions: BacktestMarginAssumptions | None = None
    research_plan: WalkForwardResearchPlan | None = None
    acceptance_criteria: WalkForwardAcceptanceCriteria | None = None
    fitter_version: Literal["frozen-strategy-fitter-v1"] | None = None
    simulation_context: dict[str, Any]
    simulation_context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    folds: list[WalkForwardFoldReport]
    validation_aggregate: WalkForwardAggregate
    test_aggregate: WalkForwardAggregate
    validation_gate: WalkForwardGateResult | None = None
    test_gate: WalkForwardGateResult | None = None
    research_decision: Literal["PASS", "FAIL"] | None = None
    promotion_status: Literal["RESEARCH_ONLY"] = "RESEARCH_ONLY"
    duration_ms: int = Field(default=0, ge=0)
    created_at: AwareDatetime


class WalkForwardArtifactMetadata(BaseModel):
    """Language-neutral metadata stored beside an immutable report payload."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    artifact_version: Literal["walk-forward-artifact-v1"] = (
        "walk-forward-artifact-v1"
    )
    experiment_id: str = Field(
        pattern=r"^walk-forward:v[12]:[a-f0-9]{64}$"
    )
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    protocol_version: Literal["walk-forward-v1"] = "walk-forward-v1"
    dataset_id: str
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    candidate_version: str = Field(min_length=3, max_length=128)
    promotion_status: Literal["RESEARCH_ONLY"] = "RESEARCH_ONLY"
    created_at: AwareDatetime
    recorded_at: AwareDatetime

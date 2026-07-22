"""Risk schemas (docs/06-risk-management.md, contracts/risk-check.schema.json)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import OrderSide, RiskStatus, utcnow

RISK_SCHEMA_VERSION = "1.0.0"
RISK_ENGINE_VERSION = "central-risk-v1"


class RiskLimits(BaseModel):
    """Global fail-safe limits. Strategy limits may only tighten these values."""

    risk_per_trade_percent: float = Field(default=1.0, gt=0, le=10)
    max_daily_drawdown_percent: float = Field(default=5.0, gt=0, le=100)
    max_total_drawdown_percent: float = Field(default=10.0, gt=0, le=100)
    max_consecutive_losses: int = Field(default=3, ge=1, le=100)
    max_open_positions: int = Field(default=3, ge=1, le=1_000)
    default_leverage: float = Field(default=1.0, ge=1, le=125)
    max_leverage: float = Field(default=5.0, ge=1, le=125)
    max_market_data_delay_ms: int = Field(default=5000, ge=0, le=300_000)
    min_risk_reward: float = Field(default=1.5, gt=0, le=100)
    max_gross_exposure_percent: float = Field(default=200.0, gt=0, le=2_000)
    max_net_exposure_percent: float = Field(default=150.0, gt=0, le=2_000)
    max_symbol_exposure_percent: float = Field(default=100.0, gt=0, le=2_000)
    max_strategy_exposure_percent: float = Field(default=100.0, gt=0, le=2_000)
    max_single_position_percent: float = Field(default=100.0, gt=0, le=1_000)
    max_symbol_concentration_percent: float = Field(default=90.0, gt=0, le=100)
    max_portfolio_var_percent: float = Field(default=5.0, gt=0, le=100)
    var_confidence: float = Field(default=0.99, ge=0.90, lt=1)
    var_lookback: int = Field(default=100, ge=20, le=2_000)
    var_min_observations: int = Field(default=30, ge=10, le=1_000)
    fallback_volatility_percent: float = Field(default=1.0, gt=0, le=100)
    approval_ttl_seconds: int = Field(default=60, ge=1, le=3_600)
    max_entry_deviation_bps: float = Field(default=100.0, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_limits(self) -> "RiskLimits":
        if self.default_leverage > self.max_leverage:
            raise ValueError("default_leverage cannot exceed max_leverage")
        if self.var_min_observations > self.var_lookback:
            raise ValueError("var_min_observations cannot exceed var_lookback")
        return self


class ApprovalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class PositionExposure(BaseModel):
    paper_order_id: str
    symbol: str
    timeframe: str
    strategy: str
    side: OrderSide
    notional: float = Field(gt=0)
    leverage: float = Field(default=1.0, ge=1)


class PortfolioRiskSnapshot(BaseModel):
    schema_version: str = RISK_SCHEMA_VERSION
    balance: float = Field(gt=0)
    position_count: int = Field(ge=0)
    gross_exposure: float = Field(ge=0)
    net_exposure: float
    proposed_notional: float = Field(ge=0)
    resulting_gross_exposure: float = Field(ge=0)
    resulting_net_exposure: float
    symbol_exposure: float = Field(ge=0)
    strategy_exposure: float = Field(ge=0)
    symbol_concentration_percent: float = Field(ge=0, le=100)


class VaRResult(BaseModel):
    schema_version: str = RISK_SCHEMA_VERSION
    method: str
    confidence: float = Field(ge=0.90, lt=1)
    observations: int = Field(ge=0)
    value_at_risk: float = Field(ge=0)
    value_at_risk_percent: float = Field(ge=0)
    expected_shortfall: float = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class OrderApproval(BaseModel):
    schema_version: str = RISK_SCHEMA_VERSION
    approval_id: str = Field(min_length=64, max_length=64)
    evaluation_id: str = Field(min_length=64, max_length=64)
    risk_check_id: str
    decision_id: str
    correlation_id: str
    request_fingerprint: str = Field(min_length=64, max_length=64)
    position_snapshot_hash: str = Field(min_length=64, max_length=64)
    symbol: str
    timeframe: str
    strategy: str
    side: OrderSide
    max_notional: float = Field(gt=0)
    max_leverage: float = Field(ge=1)
    reference_price: float = Field(gt=0)
    max_entry_deviation_bps: float = Field(ge=0)
    status: ApprovalStatus = ApprovalStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    consumed_at: datetime | None = None
    paper_order_id: str | None = None
    oms_order_id: str | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "OrderApproval":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        downstream_ids = int(bool(self.paper_order_id)) + int(bool(self.oms_order_id))
        if self.status == ApprovalStatus.CONSUMED and (
            self.consumed_at is None or downstream_ids != 1
        ):
            raise ValueError("consumed approval requires order identity and timestamp")
        if self.status != ApprovalStatus.CONSUMED and (
            self.consumed_at is not None or downstream_ids
        ):
            raise ValueError("unconsumed approval cannot reference an order")
        return self


class RiskCheck(BaseModel):
    schema_version: str = RISK_SCHEMA_VERSION
    engine_version: str = RISK_ENGINE_VERSION
    risk_check_id: str = Field(default_factory=lambda: str(uuid4()))
    evaluation_id: str = ""
    idempotency_key: str = ""
    request_fingerprint: str = ""
    decision_id: str
    correlation_id: str
    risk_status: RiskStatus
    approved: bool
    position_size: float | None = Field(default=None, ge=0)
    leverage: float | None = Field(default=None, ge=1)
    risk_percent: float | None = Field(default=None, ge=0)
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = Field(default=None, ge=0)
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    effective_limits: dict[str, float | int] = Field(default_factory=dict)
    portfolio_snapshot: PortfolioRiskSnapshot | None = None
    var_result: VaRResult | None = None
    approval_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_approval_consistency(self) -> "RiskCheck":
        is_approved = self.risk_status in (RiskStatus.APPROVED, RiskStatus.REDUCED)
        if self.approved != is_approved:
            raise ValueError("approved must match risk_status")
        if is_approved and (
            not self.approval_id
            or not self.evaluation_id
            or not self.request_fingerprint
            or not self.idempotency_key
            or self.position_size is None
            or self.portfolio_snapshot is None
            or self.var_result is None
        ):
            raise ValueError("approved risk checks require complete central approval evidence")
        if not is_approved and self.approval_id is not None:
            raise ValueError("blocked risk checks cannot carry an approval")
        return self


class RiskState(BaseModel):
    """Operational risk state used by the RiskManager."""

    daily_pnl_percent: float = 0.0
    total_drawdown_percent: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    kill_switch_active: bool = False
    blocked_operations: int = 0


class RiskControlState(BaseModel):
    schema_version: str = RISK_SCHEMA_VERSION
    active: bool = False
    revision: int = Field(default=0, ge=0)
    reason: str | None = None
    actor: str | None = None
    triggered_at: datetime | None = None
    reset_at: datetime | None = None

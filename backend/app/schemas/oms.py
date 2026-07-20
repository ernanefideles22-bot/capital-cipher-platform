"""Versioned Order Management System schemas for PAPER and TESTNET."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Exchange, OrderSide, utcnow


class ExecutionEnvironment(str, Enum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"


class OMSOrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OMSTimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    POST_ONLY = "POST_ONLY"


class OMSOrderStatus(str, Enum):
    CREATED = "CREATED"
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    QUARANTINED = "QUARANTINED"


TERMINAL_OMS_STATUSES = frozenset(
    {
        OMSOrderStatus.FILLED,
        OMSOrderStatus.CANCELED,
        OMSOrderStatus.REJECTED,
        OMSOrderStatus.EXPIRED,
        OMSOrderStatus.QUARANTINED,
    }
)


class ExecutionCommandType(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class ExecutionCommandStatus(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    COMPLETED = "COMPLETED"
    DEAD_LETTER = "DEAD_LETTER"


class ReconciliationSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ReconciliationMismatchType(str, Enum):
    LOCAL_ORDER_MISSING_AT_VENUE = "LOCAL_ORDER_MISSING_AT_VENUE"
    ORPHAN_VENUE_ORDER = "ORPHAN_VENUE_ORDER"
    ORPHAN_VENUE_FILL = "ORPHAN_VENUE_FILL"
    ORDER_STATUS_DRIFT = "ORDER_STATUS_DRIFT"
    FILLED_QUANTITY_DRIFT = "FILLED_QUANTITY_DRIFT"
    POSITION_QUANTITY_DRIFT = "POSITION_QUANTITY_DRIFT"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"


class ReconciliationRunStatus(str, Enum):
    MATCHED = "MATCHED"
    DRIFT = "DRIFT"
    FAILED = "FAILED"


class OMSOrder(BaseModel):
    schema_version: str = "1.0.0"
    oms_order_id: str = Field(default_factory=lambda: str(uuid4()))
    client_order_id: str = Field(min_length=8, max_length=36)
    decision_id: str
    risk_check_id: str
    approval_id: str = Field(min_length=64, max_length=64)
    request_fingerprint: str = Field(min_length=64, max_length=64)
    correlation_id: str
    exchange: Exchange
    environment: ExecutionEnvironment
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    strategy: str = Field(min_length=1)
    side: OrderSide
    order_type: OMSOrderType = OMSOrderType.MARKET
    time_in_force: OMSTimeInForce = OMSTimeInForce.IOC
    quantity: float = Field(gt=0)
    requested_notional: float = Field(gt=0)
    leverage: float = Field(default=1.0, ge=1)
    limit_price: float | None = Field(default=None, gt=0)
    reference_price: float = Field(gt=0)
    status: OMSOrderStatus = OMSOrderStatus.CREATED
    venue_order_id: str | None = None
    cumulative_filled_quantity: float = Field(default=0.0, ge=0)
    average_fill_price: float | None = Field(default=None, gt=0)
    rejection_reason: str | None = None
    state_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    submitted_at: datetime | None = None
    terminal_at: datetime | None = None

    @model_validator(mode="after")
    def validate_order_shape(self) -> "OMSOrder":
        if self.order_type == OMSOrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT orders require limit_price")
        if self.cumulative_filled_quantity > self.quantity + 1e-12:
            raise ValueError("filled quantity cannot exceed requested quantity")
        if self.status in TERMINAL_OMS_STATUSES and self.terminal_at is None:
            raise ValueError("terminal OMS status requires terminal_at")
        if self.status not in TERMINAL_OMS_STATUSES and self.terminal_at is not None:
            raise ValueError("non-terminal OMS status cannot have terminal_at")
        return self


class VenueOrderSnapshot(BaseModel):
    schema_version: str = "1.0.0"
    exchange: Exchange
    environment: ExecutionEnvironment = ExecutionEnvironment.TESTNET
    venue_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OMSOrderType
    status: OMSOrderStatus
    quantity: float = Field(gt=0)
    cumulative_filled_quantity: float = Field(default=0.0, ge=0)
    average_fill_price: float | None = Field(default=None, gt=0)
    observed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_fill_quantity(self) -> "VenueOrderSnapshot":
        if self.cumulative_filled_quantity > self.quantity + 1e-12:
            raise ValueError("venue filled quantity exceeds order quantity")
        return self


class ExecutionFill(BaseModel):
    schema_version: str = "1.0.0"
    fill_id: str
    oms_order_id: str | None = None
    venue_order_id: str
    client_order_id: str | None = None
    exchange: Exchange
    environment: ExecutionEnvironment
    symbol: str
    side: OrderSide
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)
    fee: float = Field(default=0.0, ge=0)
    fee_asset: str | None = None
    occurred_at: datetime
    observed_at: datetime = Field(default_factory=utcnow)


class VenuePositionSnapshot(BaseModel):
    schema_version: str = "1.0.0"
    exchange: Exchange
    environment: ExecutionEnvironment
    symbol: str
    side: OrderSide
    quantity: float = Field(ge=0)
    entry_price: float | None = Field(default=None, ge=0)
    mark_price: float | None = Field(default=None, ge=0)
    unrealized_pnl: float = 0.0
    observed_at: datetime = Field(default_factory=utcnow)


class VenueBalanceSnapshot(BaseModel):
    schema_version: str = "1.0.0"
    exchange: Exchange
    environment: ExecutionEnvironment
    asset: str
    available: float = Field(ge=0)
    locked: float = Field(default=0.0, ge=0)
    equity: float = Field(ge=0)
    observed_at: datetime = Field(default_factory=utcnow)


class VenueStateSnapshot(BaseModel):
    schema_version: str = "1.0.0"
    exchange: Exchange
    environment: ExecutionEnvironment
    orders: list[VenueOrderSnapshot] = Field(default_factory=list)
    fills: list[ExecutionFill] = Field(default_factory=list)
    positions: list[VenuePositionSnapshot] = Field(default_factory=list)
    balances: list[VenueBalanceSnapshot] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_venue_scope(self) -> "VenueStateSnapshot":
        evidence = [
            *self.orders,
            *self.fills,
            *self.positions,
            *self.balances,
        ]
        if any(
            item.exchange != self.exchange
            or item.environment != self.environment
            for item in evidence
        ):
            raise ValueError("venue evidence crosses exchange/environment scope")
        return self


class ExecutionCommand(BaseModel):
    schema_version: str = "1.0.0"
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    oms_order_id: str
    command_type: ExecutionCommandType
    status: ExecutionCommandStatus = ExecutionCommandStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1, le=1)
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    available_at: datetime = Field(default_factory=utcnow)
    last_error_type: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ExecutionCommand":
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count exceeds the single write attempt")
        if self.status == ExecutionCommandStatus.PENDING and (
            self.leased_by is not None
            or self.lease_expires_at is not None
            or self.completed_at is not None
        ):
            raise ValueError("pending command cannot have lease/completion data")
        if self.status == ExecutionCommandStatus.LEASED and (
            not self.leased_by
            or self.lease_expires_at is None
            or self.completed_at is not None
        ):
            raise ValueError("leased command requires an active lease")
        if self.status in {
            ExecutionCommandStatus.COMPLETED,
            ExecutionCommandStatus.DEAD_LETTER,
        } and (
            self.leased_by is not None
            or self.lease_expires_at is not None
            or self.completed_at is None
        ):
            raise ValueError("finished command has invalid lifecycle data")
        return self


class ReconciliationMismatch(BaseModel):
    schema_version: str = "1.0.0"
    mismatch_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    mismatch_type: ReconciliationMismatchType
    severity: ReconciliationSeverity
    exchange: Exchange
    environment: ExecutionEnvironment
    oms_order_id: str | None = None
    venue_order_id: str | None = None
    symbol: str | None = None
    expected: dict = Field(default_factory=dict)
    observed: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ReconciliationRun(BaseModel):
    schema_version: str = "1.0.0"
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    exchange: Exchange
    environment: ExecutionEnvironment
    status: ReconciliationRunStatus
    local_order_count: int = Field(default=0, ge=0)
    venue_order_count: int = Field(default=0, ge=0)
    fill_count: int = Field(default=0, ge=0)
    position_count: int = Field(default=0, ge=0)
    balance_count: int = Field(default=0, ge=0)
    mismatch_count: int = Field(default=0, ge=0)
    critical_mismatch_count: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    error_type: str | None = None

    @model_validator(mode="after")
    def validate_completion(self) -> "ReconciliationRun":
        if self.critical_mismatch_count > self.mismatch_count:
            raise ValueError(
                "critical mismatch count exceeds total mismatch count"
            )
        if self.completed_at is None:
            raise ValueError("reconciliation result requires completed_at")
        if (
            self.status == ReconciliationRunStatus.FAILED
            and not self.error_type
        ):
            raise ValueError("failed reconciliation requires error_type")
        if (
            self.status != ReconciliationRunStatus.FAILED
            and self.error_type is not None
        ):
            raise ValueError("successful reconciliation cannot have error_type")
        return self

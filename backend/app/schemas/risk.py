"""Risk schemas (docs/06-risk-management.md, contracts/risk-check.schema.json)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import RiskStatus, utcnow


class RiskLimits(BaseModel):
    """Initial paper-trading limits from docs/06-risk-management.md."""

    risk_per_trade_percent: float = 1.0
    max_daily_drawdown_percent: float = 5.0
    max_total_drawdown_percent: float = 10.0
    max_consecutive_losses: int = 3
    max_open_positions: int = 3
    default_leverage: float = 1.0
    max_leverage: float = 5.0
    max_market_data_delay_ms: int = 5000
    min_risk_reward: float = 1.5


class RiskCheck(BaseModel):
    risk_check_id: str = Field(default_factory=lambda: str(uuid4()))
    decision_id: str
    correlation_id: str
    risk_status: RiskStatus
    approved: bool
    position_size: float | None = Field(default=None, ge=0)
    risk_percent: float | None = Field(default=None, ge=0)
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = Field(default=None, ge=0)
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class RiskState(BaseModel):
    """Operational risk state used by the RiskManager."""

    daily_pnl_percent: float = 0.0
    total_drawdown_percent: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    kill_switch_active: bool = False
    blocked_operations: int = 0

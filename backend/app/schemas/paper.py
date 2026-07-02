"""Paper trading schemas (docs/18-paper-trading.md, contracts/paper-order.schema.json)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import Exchange, OrderSide, PaperOrderStatus, utcnow


class PaperOrder(BaseModel):
    paper_order_id: str = Field(default_factory=lambda: str(uuid4()))
    decision_id: str
    risk_check_id: str
    correlation_id: str
    exchange: Exchange
    symbol: str = Field(min_length=1)
    timeframe: str | None = None
    side: OrderSide
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    position_size: float = Field(ge=0)
    status: PaperOrderStatus = PaperOrderStatus.CREATED
    fees_estimated: float = Field(ge=0, default=0.0)
    slippage_estimated: float = Field(ge=0, default=0.0)
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    pnl: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PaperPerformance(BaseModel):
    """Performance metrics (docs/18)."""

    total_trades: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fees_total: float = 0.0
    slippage_total: float = 0.0
    max_drawdown_percent: float = 0.0
    consecutive_losses: int = 0
    balance: float = 0.0
    initial_balance: float = 0.0


class SymbolPerformance(BaseModel):
    """Per-symbol / per-timeframe breakdown (docs/07 Fase 2, docs/18)."""

    key: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float | None = None


class EquityPoint(BaseModel):
    timestamp: str
    balance: float

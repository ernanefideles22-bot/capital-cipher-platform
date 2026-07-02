"""Shared enums and helpers derived from /contracts (ADR-003 contract-first)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Exchange(str, Enum):
    BINANCE = "BINANCE"
    BYBIT = "BYBIT"


class AgentStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    BLOCK = "BLOCK"
    NEUTRAL = "NEUTRAL"


class CandidateAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WAIT = "WAIT"
    BLOCK = "BLOCK"


class RiskStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REDUCED = "REDUCED"
    BLOCKED = "BLOCKED"
    KILL_SWITCH = "KILL_SWITCH"


class PaperOrderStatus(str, Enum):
    CREATED = "CREATED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNDEFINED = "UNDEFINED"


class DataQualityStatus(str, Enum):
    VALID = "VALID"
    WARNING = "WARNING"
    SUSPECT = "SUSPECT"
    INVALID = "INVALID"

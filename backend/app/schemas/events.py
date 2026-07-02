"""Event schemas (docs/10-system-events.md, contracts/system-event.schema.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import utcnow


class SystemEvent(BaseModel):
    """Common envelope for every system event."""

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str
    event_type: str
    source: str
    timestamp: datetime = Field(default_factory=utcnow)
    version: str = "1.0"
    payload: dict[str, Any] = Field(default_factory=dict)


class BusMessage(BaseModel):
    """Message bus envelope (docs/23-message-bus.md)."""

    message_id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str
    topic: str
    event_type: str
    source: str
    timestamp: datetime = Field(default_factory=utcnow)
    version: str = "1.0"
    payload: dict[str, Any] = Field(default_factory=dict)


class EventTypes:
    # Market
    MARKET_CONNECTED = "MARKET_CONNECTED"
    MARKET_DISCONNECTED = "MARKET_DISCONNECTED"
    CANDLE_CLOSED = "CANDLE_CLOSED"
    TRADE_RECEIVED = "TRADE_RECEIVED"
    ORDERBOOK_UPDATED = "ORDERBOOK_UPDATED"
    # Agents
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    # Decisions
    DECISION_CANDIDATE_CREATED = "DECISION_CANDIDATE_CREATED"
    DECISION_SENT_TO_RISK = "DECISION_SENT_TO_RISK"
    DECISION_APPROVED = "DECISION_APPROVED"
    DECISION_REDUCED = "DECISION_REDUCED"
    DECISION_BLOCKED = "DECISION_BLOCKED"
    # Risk
    RISK_CHECK_STARTED = "RISK_CHECK_STARTED"
    RISK_CHECK_COMPLETED = "RISK_CHECK_COMPLETED"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    # Paper trading
    PAPER_ORDER_CREATED = "PAPER_ORDER_CREATED"
    PAPER_ORDER_FILLED = "PAPER_ORDER_FILLED"
    PAPER_ORDER_CLOSED = "PAPER_ORDER_CLOSED"
    PAPER_ORDER_CANCELLED = "PAPER_ORDER_CANCELLED"
    # Audit
    AUDIT_LOG_CREATED = "AUDIT_LOG_CREATED"
    AUDIT_LOG_FAILED = "AUDIT_LOG_FAILED"
    # System
    SYSTEM_STARTED = "SYSTEM_STARTED"
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    SYSTEM_MODE_CHANGED = "SYSTEM_MODE_CHANGED"

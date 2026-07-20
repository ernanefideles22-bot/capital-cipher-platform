"""Event schemas (docs/10-system-events.md, contracts/system-event.schema.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import utcnow

CONTRACT_VERSION = "1.0.0"


class SystemEvent(BaseModel):
    """Common envelope for every system event."""

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    correlation_id: str = Field(min_length=1)
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    source: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utcnow)
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)


class BusMessage(BaseModel):
    """Message bus envelope (docs/23-message-bus.md)."""

    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    correlation_id: str = Field(min_length=1)
    topic: str = Field(pattern=r"^[a-z][a-z0-9_.-]+\.v[1-9][0-9]*$")
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    source: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utcnow)
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)


class EventTypes:
    # Market
    RAW_MARKET_EVENT_RECEIVED = "RAW_MARKET_EVENT_RECEIVED"
    MARKET_CONNECTED = "MARKET_CONNECTED"
    MARKET_DISCONNECTED = "MARKET_DISCONNECTED"
    CANDLE_CLOSED = "CANDLE_CLOSED"
    TRADE_RECEIVED = "TRADE_RECEIVED"
    ORDERBOOK_UPDATED = "ORDERBOOK_UPDATED"
    # Agents
    AGENT_REQUESTED = "AGENT_REQUESTED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_RETRY_SCHEDULED = "AGENT_RETRY_SCHEDULED"
    AGENT_DEAD_LETTERED = "AGENT_DEAD_LETTERED"
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
    # Order management
    OMS_ORDER_ACCEPTED = "OMS_ORDER_ACCEPTED"
    OMS_ORDER_RECONCILED = "OMS_ORDER_RECONCILED"
    # Audit
    AUDIT_LOG_CREATED = "AUDIT_LOG_CREATED"
    AUDIT_LOG_FAILED = "AUDIT_LOG_FAILED"
    # System
    SYSTEM_STARTED = "SYSTEM_STARTED"
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    SYSTEM_MODE_CHANGED = "SYSTEM_MODE_CHANGED"

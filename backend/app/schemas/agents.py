"""Agent contracts (docs/11-agent-contracts.md, contracts/agent-output.schema.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import AgentStatus, Signal, utcnow


class AgentInput(BaseModel):
    """Base input contract for every agent."""

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str
    agent_name: str
    timestamp: datetime = Field(default_factory=utcnow)
    symbol: str
    timeframe: str
    market_context: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(BaseModel):
    """Base output contract — mirrors contracts/agent-output.schema.json."""

    agent_name: str = Field(min_length=1)
    status: AgentStatus
    signal: Signal
    confidence: int = Field(ge=0, le=100)
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0, default=0)
    created_at: datetime = Field(default_factory=utcnow)


class AgentRegistration(BaseModel):
    """Agent registration payload (docs/28-agent-lifecycle.md)."""

    agent_name: str
    version: str = "1.0.0"
    description: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    output_contract: str = "AgentOutputV1"
    critical: bool = False
    timeout_ms: int = 5000
    enabled: bool = True


class AgentHealth(BaseModel):
    name: str
    status: str
    version: str
    critical: bool
    enabled: bool
    last_run_at: datetime | None = None
    last_failure_at: datetime | None = None
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    total_runs: int = 0
    total_failures: int = 0
    last_signal: str | None = None
    last_confidence: int | None = None

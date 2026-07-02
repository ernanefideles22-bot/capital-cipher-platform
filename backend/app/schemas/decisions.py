"""Decision schemas (docs/25-decision-engine.md, contracts/decision.schema.json)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.schemas.common import CandidateAction, RiskStatus, utcnow


class Decision(BaseModel):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str
    symbol: str = Field(min_length=1)
    timeframe: str
    candidate_action: CandidateAction
    confidence: int = Field(ge=0, le=100)
    strategy: str = "SCALP_15M"
    reason: str = ""
    agent_summary: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    risk_status: RiskStatus = RiskStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)

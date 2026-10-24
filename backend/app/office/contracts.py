"""Versioned analytical context for the future asynchronous office boundary.

These contracts do not approve orders. Consumers must still consult the central
risk manager, reconciled portfolio and OMS before any PAPER execution.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Department(str, Enum):
    MARKET = "MARKET_INTELLIGENCE"
    TECHNICAL = "ADVANCED_TECHNICAL"
    QUANT = "QUANT_RESEARCH"
    STRATEGY = "TRADING_STRATEGIES"
    EXECUTION = "MICROSTRUCTURE_EXECUTION"
    CRYPTO = "CRYPTO_INTELLIGENCE"
    MACRO = "MACRO_NEWS"
    PORTFOLIO = "PORTFOLIO_MANAGEMENT"
    RISK = "RISK_SECURITY"
    LEARNING = "LEARNING_AUDIT"


class OfficeAnalysis(BaseModel):
    """Evidence-only message, never an order, risk approval or strategy update.

Validity is [published_at, expires_at), additionally bound to the input candle
by as_of. An old candle published late must not receive a fresh lifetime.
"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["office-analysis-v1"] = "office-analysis-v1"
    execution_mode: Literal["PAPER"] = "PAPER"
    order_authority: Literal[False] = False
    risk_authority: Literal[False] = False
    promotion_authority: Literal[False] = False
    analysis_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=36)
    agent_name: str = Field(min_length=3, max_length=128)
    agent_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    department: Department
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: Literal["15m", "1h", "4h"]
    direction: Literal["BUY", "SELL", "HOLD", "WAIT", "NEUTRAL"]
    confirmation: tuple[str, ...] = Field(min_length=1, max_length=32)
    invalidation: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    as_of: AwareDatetime
    published_at: AwareDatetime
    expires_at: AwareDatetime
    max_age_seconds: int = Field(gt=0, le=14_400)

    @model_validator(mode="after")
    def validate_lifetime(self) -> "OfficeAnalysis":
        if not self.as_of <= self.published_at < self.expires_at:
            raise ValueError("Require as_of <= published_at < expires_at")
        if (self.expires_at - self.as_of).total_seconds() > self.max_age_seconds:
            raise ValueError("Lifetime exceeds the input evidence age budget")
        for values in (self.confirmation, self.invalidation, self.evidence_ids):
            if any(not value.strip() or len(value) > 1_000 for value in values):
                raise ValueError("Criteria and evidence references must be nonempty and bounded")
        return self

    def usable_at(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Evaluation time must be timezone-aware")
        return self.published_at <= now < self.expires_at

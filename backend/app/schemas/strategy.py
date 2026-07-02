"""Strategy schemas (docs/26-strategy-engine.md, contracts/strategy.schema.json)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.schemas.common import MarketRegime


class RiskProfileName(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"


class RiskProfile(BaseModel):
    """Risk profiles from docs/26. Phase 1-2: simulation only.

    Global limits in docs/06 keep authority: the RiskManager caps whatever a
    strategy asks for (ADR-001 — strategy never overrides risk).
    """

    name: RiskProfileName
    risk_per_trade_percent: float
    max_open_positions: int
    risk_reward_min: float


RISK_PROFILES: dict[RiskProfileName, RiskProfile] = {
    RiskProfileName.CONSERVATIVE: RiskProfile(
        name=RiskProfileName.CONSERVATIVE,
        risk_per_trade_percent=0.5,
        max_open_positions=3,
        risk_reward_min=1.8,
    ),
    RiskProfileName.MODERATE: RiskProfile(
        name=RiskProfileName.MODERATE,
        risk_per_trade_percent=1.0,
        max_open_positions=8,
        risk_reward_min=2.2,
    ),
    RiskProfileName.AGGRESSIVE: RiskProfile(
        name=RiskProfileName.AGGRESSIVE,
        risk_per_trade_percent=1.5,
        max_open_positions=15,
        risk_reward_min=2.8,
    ),
}


class StrategyConfig(BaseModel):
    """Versioned strategy definition (docs/26)."""

    strategy_id: str
    version: str = "v1"
    enabled: bool = True
    symbols: list[str]
    timeframe: str
    minimum_confidence: int = Field(ge=0, le=100, default=75)
    risk_profile: RiskProfileName = RiskProfileName.MODERATE
    required_agents: list[str] = Field(default_factory=lambda: ["QuantAgent", "TrendAgent"])
    optional_agents: list[str] = Field(default_factory=list)
    allowed_regimes: list[MarketRegime] = Field(
        default_factory=lambda: [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]
    )
    reduced_regimes: list[MarketRegime] = Field(default_factory=lambda: [MarketRegime.RANGE])
    blocked_regimes: list[MarketRegime] = Field(
        default_factory=lambda: [MarketRegime.HIGH_VOLATILITY]
    )
    range_confidence_penalty: int = 10

    @property
    def versioned_id(self) -> str:
        return f"{self.strategy_id}_{self.version}"


class StrategyEvaluation(BaseModel):
    """Result of applying strategy rules to a market context."""

    strategy_id: str
    versioned_id: str
    allowed: bool
    reduced: bool = False
    minimum_confidence: int
    risk_profile: RiskProfileName
    reason: str = ""

"""Strategy Engine (docs/26-strategy-engine.md).

Selects the active strategy for a symbol/timeframe, applies regime rules and
exposes the effective minimum confidence and risk profile. Strategies are
versioned; changing parameters requires a new version.
"""

from __future__ import annotations

from app.schemas.common import MarketRegime
from app.schemas.strategy import (
    RISK_PROFILES,
    RiskProfile,
    RiskProfileName,
    StrategyConfig,
    StrategyEvaluation,
)

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def default_strategies() -> list[StrategyConfig]:
    """Initial strategies from docs/26."""
    return [
        StrategyConfig(
            strategy_id="SCALP_15M",
            version="v1",
            symbols=DEFAULT_SYMBOLS,
            timeframe="15m",
            minimum_confidence=75,
            risk_profile=RiskProfileName.MODERATE,
        ),
        StrategyConfig(
            strategy_id="DAY_1H",
            version="v1",
            symbols=DEFAULT_SYMBOLS,
            timeframe="1h",
            minimum_confidence=75,
            risk_profile=RiskProfileName.MODERATE,
        ),
        StrategyConfig(
            strategy_id="SWING_4H",
            version="v1",
            symbols=DEFAULT_SYMBOLS,
            timeframe="4h",
            minimum_confidence=70,
            risk_profile=RiskProfileName.CONSERVATIVE,
        ),
    ]


class StrategyEngine:
    def __init__(self, strategies: list[StrategyConfig] | None = None) -> None:
        self._strategies: dict[str, StrategyConfig] = {}
        for strategy in strategies if strategies is not None else default_strategies():
            self.register(strategy)

    # -- management (docs/26: enable/disable, versioning) ----------------------
    def register(self, strategy: StrategyConfig) -> None:
        self._strategies[strategy.versioned_id] = strategy

    def list(self) -> list[StrategyConfig]:
        return list(self._strategies.values())

    def set_enabled(self, versioned_id: str, enabled: bool) -> StrategyConfig:
        strategy = self._strategies[versioned_id]
        updated = strategy.model_copy(update={"enabled": enabled})
        self._strategies[versioned_id] = updated
        return updated

    def select(self, symbol: str, timeframe: str) -> StrategyConfig | None:
        """Pick the enabled strategy matching symbol and timeframe."""
        for strategy in self._strategies.values():
            if (
                strategy.enabled
                and strategy.timeframe == timeframe
                and symbol.upper() in strategy.symbols
            ):
                return strategy
        return None

    def risk_profile(self, strategy: StrategyConfig) -> RiskProfile:
        return RISK_PROFILES[strategy.risk_profile]

    # -- evaluation --------------------------------------------------------------
    def evaluate(
        self, *, symbol: str, timeframe: str, regime: MarketRegime
    ) -> StrategyEvaluation:
        """Apply strategy regime rules (docs/26).

        HIGH_VOLATILITY blocks, RANGE reduces aggressiveness (higher minimum
        confidence), unknown symbol/timeframe blocks.
        """
        strategy = self.select(symbol, timeframe)
        if strategy is None:
            return StrategyEvaluation(
                strategy_id="NONE",
                versioned_id="NONE",
                allowed=False,
                minimum_confidence=100,
                risk_profile=RiskProfileName.CONSERVATIVE,
                reason=f"No enabled strategy for {symbol} {timeframe}",
            )
        if regime in strategy.blocked_regimes:
            return StrategyEvaluation(
                strategy_id=strategy.strategy_id,
                versioned_id=strategy.versioned_id,
                allowed=False,
                minimum_confidence=strategy.minimum_confidence,
                risk_profile=strategy.risk_profile,
                reason=f"Regime {regime.value} blocked for {strategy.versioned_id}",
            )
        if regime in strategy.reduced_regimes:
            return StrategyEvaluation(
                strategy_id=strategy.strategy_id,
                versioned_id=strategy.versioned_id,
                allowed=True,
                reduced=True,
                minimum_confidence=min(
                    100, strategy.minimum_confidence + strategy.range_confidence_penalty
                ),
                risk_profile=strategy.risk_profile,
                reason=f"Regime {regime.value}: reduced aggressiveness",
            )
        return StrategyEvaluation(
            strategy_id=strategy.strategy_id,
            versioned_id=strategy.versioned_id,
            allowed=True,
            minimum_confidence=strategy.minimum_confidence,
            risk_profile=strategy.risk_profile,
            reason=f"Regime {regime.value} allowed",
        )

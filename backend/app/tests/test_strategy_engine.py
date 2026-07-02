"""Strategy Engine tests (docs/26)."""

from __future__ import annotations

from app.schemas.common import MarketRegime
from app.schemas.strategy import RiskProfileName
from app.strategy.engine import StrategyEngine


def test_default_strategies_registered():
    engine = StrategyEngine()
    ids = {s.strategy_id for s in engine.list()}
    assert ids == {"SCALP_15M", "DAY_1H", "SWING_4H"}


def test_select_by_symbol_and_timeframe():
    engine = StrategyEngine()
    assert engine.select("BTCUSDT", "15m").strategy_id == "SCALP_15M"
    assert engine.select("ETHUSDT", "1h").strategy_id == "DAY_1H"
    assert engine.select("BTCUSDT", "5m") is None
    assert engine.select("DOGEUSDT", "15m") is None


def test_high_volatility_blocks():
    engine = StrategyEngine()
    result = engine.evaluate(
        symbol="BTCUSDT", timeframe="15m", regime=MarketRegime.HIGH_VOLATILITY
    )
    assert result.allowed is False


def test_range_reduces_aggressiveness():
    engine = StrategyEngine()
    normal = engine.evaluate(symbol="BTCUSDT", timeframe="15m", regime=MarketRegime.BULL_TREND)
    reduced = engine.evaluate(symbol="BTCUSDT", timeframe="15m", regime=MarketRegime.RANGE)
    assert reduced.allowed and reduced.reduced
    assert reduced.minimum_confidence > normal.minimum_confidence


def test_unknown_context_blocks():
    engine = StrategyEngine()
    result = engine.evaluate(symbol="XRPUSDT", timeframe="15m", regime=MarketRegime.BULL_TREND)
    assert result.allowed is False


def test_disabled_strategy_not_selected():
    engine = StrategyEngine()
    engine.set_enabled("SCALP_15M_v1", False)
    assert engine.select("BTCUSDT", "15m") is None


def test_versioning_creates_new_entry():
    engine = StrategyEngine()
    base = engine.select("BTCUSDT", "15m")
    v2 = base.model_copy(update={"version": "v2", "minimum_confidence": 80})
    engine.register(v2)
    assert len([s for s in engine.list() if s.strategy_id == "SCALP_15M"]) == 2


def test_swing_uses_conservative_profile():
    engine = StrategyEngine()
    strategy = engine.select("BTCUSDT", "4h")
    profile = engine.risk_profile(strategy)
    assert profile.name == RiskProfileName.CONSERVATIVE
    assert profile.risk_per_trade_percent == 0.5

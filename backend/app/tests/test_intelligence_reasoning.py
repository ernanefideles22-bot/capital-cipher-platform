"""Contract tests for read-only market reasoning."""
import pytest

from app.agents.intelligence import MarketSnapshot, analyze_market


def snapshot(**overrides):
    values = dict(symbol="BTCUSDT", timeframe="15m", observed_at="2026-10-15T15:00:00Z",
                  close=82318.2, ma_fast=82321.1, ma_medium=82296.3,
                  ma_slow=81702.0, recent_high=82666.3, recent_low=79797.0,
                  volume=2.601, average_volume=40.530, source_id="example-screenshot")
    values.update(overrides)
    return MarketSnapshot(**values)


def test_reasoning_is_explainable_and_non_executable():
    result = analyze_market(snapshot())
    assert result.bias == "BULLISH"
    assert result.action == "WAIT"
    assert result.decision_authority is False
    assert result.order_authority is False
    assert result.counter_evidence
    assert "verified_news" in result.missing_evidence
    assert result.source_id == "example-screenshot"


def test_invalid_prices_rejected():
    with pytest.raises(ValueError):
        snapshot(close=float("nan"))
    with pytest.raises(ValueError):
        snapshot(recent_low=90000)


def test_mixed_market_does_not_invent_direction():
    result = analyze_market(snapshot(ma_fast=82000, ma_medium=82500, ma_slow=81000))
    assert result.bias == "MIXED"
    assert result.action == "WAIT"

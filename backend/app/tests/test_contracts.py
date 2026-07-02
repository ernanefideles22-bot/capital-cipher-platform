"""Contract tests (docs/22): agents must respect input/output contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.agents import AgentOutput
from app.schemas.common import AgentStatus, Signal
from app.schemas.decisions import Decision
from app.schemas.market import Candle


def test_agent_output_valid_contract():
    output = AgentOutput(
        agent_name="QuantAgent",
        status=AgentStatus.COMPLETED,
        signal=Signal.BUY,
        confidence=78,
        reason="test",
    )
    assert output.confidence == 78


@pytest.mark.parametrize("confidence", [-1, 101, 150])
def test_agent_output_confidence_bounds(confidence):
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_name="QuantAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.BUY,
            confidence=confidence,
            reason="test",
        )


def test_agent_output_rejects_invalid_signal():
    with pytest.raises(ValidationError):
        AgentOutput(
            agent_name="QuantAgent",
            status=AgentStatus.COMPLETED,
            signal="MOON",
            confidence=50,
            reason="test",
        )


def test_decision_requires_correlation_id():
    with pytest.raises(ValidationError):
        Decision(
            symbol="BTCUSDT",
            timeframe="15m",
            candidate_action="BUY",
            confidence=80,
        )


def test_candle_invariant_high_low():
    with pytest.raises(ValidationError):
        Candle(
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            open=100,
            high=90,  # high < open: invalid
            low=95,
            close=99,
            volume=10,
            closed_at="2026-07-01T12:00:00Z",
        )


def test_candle_rejects_negative_volume():
    with pytest.raises(ValidationError):
        Candle(
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            open=100,
            high=101,
            low=99,
            close=100,
            volume=-1,
            closed_at="2026-07-01T12:00:00Z",
        )

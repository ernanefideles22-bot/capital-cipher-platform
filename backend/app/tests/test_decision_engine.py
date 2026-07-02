"""Decision Engine tests (docs/25)."""

from __future__ import annotations

from uuid import uuid4

from app.orchestrator.decision_engine import DecisionEngine
from app.schemas.agents import AgentOutput
from app.schemas.common import AgentStatus, CandidateAction, Signal


def output(name: str, signal: Signal, confidence: int, status=AgentStatus.COMPLETED) -> AgentOutput:
    return AgentOutput(
        agent_name=name, status=status, signal=signal, confidence=confidence, reason="test"
    )


def consolidate(engine, outputs, **kw):
    return engine.consolidate(
        correlation_id=str(uuid4()),
        symbol="BTCUSDT",
        timeframe="15m",
        agent_outputs=outputs,
        **kw,
    )


def full_set(quant_signal=Signal.BUY, quant_conf=90, trend_signal=Signal.BUY, trend_conf=85):
    return [
        output("MarketDataAgent", Signal.NEUTRAL, 100),
        output("QuantAgent", quant_signal, quant_conf),
        output("TrendAgent", trend_signal, trend_conf),
    ]


def test_aligned_signals_produce_buy():
    engine = DecisionEngine()
    decision = consolidate(engine, full_set())
    assert decision.candidate_action == CandidateAction.BUY
    assert decision.confidence >= 70


def test_strong_conflict_produces_wait_not_average():
    engine = DecisionEngine()
    decision = consolidate(engine, full_set(Signal.BUY, 85, Signal.SELL, 80))
    assert decision.candidate_action == CandidateAction.WAIT
    assert "CONFLICTING_SIGNALS" in decision.warnings


def test_missing_critical_agent_blocks():
    engine = DecisionEngine()
    decision = consolidate(engine, [output("QuantAgent", Signal.BUY, 90)])
    assert decision.candidate_action == CandidateAction.BLOCK


def test_failed_critical_agent_blocks():
    engine = DecisionEngine()
    outputs = full_set()
    outputs[1] = output("QuantAgent", Signal.NEUTRAL, 0, status=AgentStatus.FAILED)
    decision = consolidate(engine, outputs)
    assert decision.candidate_action == CandidateAction.BLOCK


def test_low_data_quality_blocks():
    engine = DecisionEngine()
    decision = consolidate(engine, full_set(), data_quality_score=40)
    assert decision.candidate_action == CandidateAction.BLOCK


def test_low_confidence_becomes_wait():
    engine = DecisionEngine(minimum_candidate_confidence=70)
    decision = consolidate(engine, full_set(Signal.BUY, 45, Signal.HOLD, 50))
    assert decision.candidate_action in (CandidateAction.WAIT, CandidateAction.HOLD)


def test_market_data_block_is_respected():
    engine = DecisionEngine()
    outputs = full_set()
    outputs[0] = output("MarketDataAgent", Signal.BLOCK, 0)
    decision = consolidate(engine, outputs)
    assert decision.candidate_action == CandidateAction.BLOCK


def test_decision_always_has_agent_summary():
    engine = DecisionEngine()
    decision = consolidate(engine, full_set())
    assert len(decision.agent_summary) == 3

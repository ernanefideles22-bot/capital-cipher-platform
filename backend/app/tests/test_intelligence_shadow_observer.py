"""Non-invasive PAPER trace observer contract tests."""
import pytest

from app.agents.intelligence.shadow_observer import summarize_cohort, summarize_trace
from app.schemas.agents import AgentExecutionJob, AgentExecutionTrace, AgentInput, AgentOutput
from app.schemas.common import utcnow


def make_trace(name="QuantAgent", role="PRIMARY"):
    now = utcnow()
    agent_input = AgentInput(
        agent_name=name, correlation_id="test", symbol="BTCUSDT", timeframe="15m"
    )
    output = AgentOutput(
        agent_name=name, status="COMPLETED", signal="NEUTRAL",
        confidence=50, reason="test", evidence={"secret_raw": "not copied"}
    )
    job = AgentExecutionJob(
        execution_id="a" * 64, request_fingerprint="a" * 64,
        idempotency_key="test", correlation_id="test", agent_name=name,
        agent_version="1.0.0", agent_definition_hash="b" * 64,
        decision_role=role, critical=False, input=agent_input,
        status="COMPLETED", attempt_count=1, max_attempts=3,
        available_at=now, output=output, created_at=now,
        updated_at=now, completed_at=now,
    )
    return AgentExecutionTrace(job=job, attempts=[], memory=[])


def test_terminal_summary_never_exposes_raw_evidence():
    result = summarize_trace(make_trace())
    assert result.symbol == "BTCUSDT"
    assert not result.decision_authority
    assert not result.order_authority
    assert "secret_raw" not in repr(result)


def test_incomplete_cohort_rejected():
    with pytest.raises(ValueError, match="Incomplete"):
        summarize_cohort([make_trace()])

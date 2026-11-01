"""E1.5 candidate visibility: historical coverage is not OOS or authorization."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.strategy.paper_candidate_coverage_e15 import describe_candidate_visibility


NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def d(i, action, *, risk="PENDING"):
    return SimpleNamespace(
        id=i, correlation_id=f"corr-{i}", candidate_action=action,
        risk_status=risk, created_at=NOW,
    )


def test_nonactionable_decisions_do_not_require_risk_or_order():
    rows = [d("hold", "HOLD"), d("buy", "BUY", risk="APPROVED")]
    risks = [SimpleNamespace(decision_id="buy")]
    orders = [SimpleNamespace(decision_id="buy")]
    agents = [
        SimpleNamespace(correlation_id="corr-hold", created_at=NOW-timedelta(seconds=1)),
        SimpleNamespace(correlation_id="corr-buy", created_at=NOW-timedelta(seconds=1)),
    ]
    result = describe_candidate_visibility(
        rows, risks, orders, agents, sampled_limit=1000,
        has_more_decisions=False,
    )
    assert result["mode"] == "HISTORICAL_BOUNDED_READ_ONLY_PREFLIGHT"
    assert not result["prospective_collection_running"]
    assert not result["candidate_visibility_complete"]
    assert result["candidate_actions"] == {"BUY": 1, "HOLD": 1}
    assert result["risk_check_visibility"] == {
        "has_risk_check": 1, "no_visible_risk_check": 1,
    }
    assert result["paper_order_visibility"] == {
        "has_paper_order": 1, "no_visible_paper_order": 1,
    }
    assert result["predecision_agent_visibility"] == {
        "has_predecision_agent_output": 2,
    }


def test_agent_after_decision_does_not_count():
    r=describe_candidate_visibility(
        [d("a","SELL")], [], [],
        [SimpleNamespace(correlation_id="corr-a", created_at=NOW+timedelta(seconds=1))],
        sampled_limit=1, has_more_decisions=True,
    )
    assert r["has_more_decisions_than_limit"]
    assert r["predecision_agent_visibility"] == {
        "no_visible_predecision_agent_output": 1,
    }


def test_empty_and_bad_samples_fail_closed():
    result=describe_candidate_visibility([],[],[],[],sampled_limit=1000,
                                          has_more_decisions=False)
    assert result["sampled_decisions"] == 0
    assert result["oldest_sampled_decision_utc"] is None
    with pytest.raises(ValueError, match="Duplicate"):
        describe_candidate_visibility([d("same","BUY"), d("same","HOLD")],
                                     [],[],[],sampled_limit=1000,has_more_decisions=False)
    with pytest.raises(ValueError, match="Unbounded"):
        describe_candidate_visibility([d("first","BUY"),d("second","SELL")],
                                     [],[],[],sampled_limit=1,has_more_decisions=False)
    with pytest.raises(ValueError, match="timezone"):
        bad=d("bad","BUY")
        bad.created_at=datetime(2026,10,31,12)
        describe_candidate_visibility([bad],[],[],[],sampled_limit=1,
                                     has_more_decisions=False)


def test_modern_agent_jobs_reconcile_missing_legacy_outputs():
    decision = d("modern", "BUY", risk="APPROVED")
    completed = SimpleNamespace(
        correlation_id=decision.correlation_id,
        status="COMPLETED",
        completed_at=NOW-timedelta(milliseconds=250),
        output_payload={"signal": "BUY"},
        decision_role="PRIMARY",
    )
    shadow = SimpleNamespace(
        correlation_id=decision.correlation_id,
        status="COMPLETED",
        completed_at=NOW-timedelta(milliseconds=100),
        output_payload={"signal": "HOLD"},
        decision_role="SHADOW",
    )
    result = describe_candidate_visibility(
        [decision], [], [], [], execution_jobs=[completed, shadow],
        sampled_limit=1000, has_more_decisions=False,
    )
    assert result["predecision_agent_visibility"] == {
        "no_visible_predecision_agent_output": 1,
    }
    assert result["execution_job_visibility"] == {
        "has_completed_predecision_job": 1,
    }
    assert result["completed_job_role_visibility"] == {"PRIMARY": 1, "SHADOW": 1}
    funnel = result["action_funnel"]["BUY"]
    assert funnel["decisions"] == 1
    assert funnel["predecision_evidence_in_either_source"] == 1
    assert funnel["no_visible_risk_check"] == 1
    assert funnel["no_visible_paper_order"] == 1


def test_late_or_failed_job_is_not_predecision_evidence():
    decision = d("late", "SELL", risk="BLOCKED")
    late = SimpleNamespace(
        correlation_id=decision.correlation_id,
        status="COMPLETED",
        completed_at=NOW+timedelta(seconds=1),
        output_payload={"signal": "SELL"},
        decision_role="PRIMARY",
    )
    failed = SimpleNamespace(
        correlation_id=decision.correlation_id,
        status="DEAD_LETTER",
        completed_at=NOW-timedelta(seconds=1),
        output_payload={"signal": "BLOCK"},
        decision_role="PRIMARY",
    )
    result = describe_candidate_visibility(
        [decision], [], [], [], execution_jobs=[late, failed],
        sampled_limit=1000, has_more_decisions=False,
    )
    assert result["execution_job_visibility"] == {
        "job_visible_but_not_completed_before_decision": 1,
    }
    assert result["action_funnel"]["SELL"]["no_predecision_evidence_in_either_source"] == 1
    assert result["action_funnel"]["SELL"]["risk_status_BLOCKED"] == 1

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

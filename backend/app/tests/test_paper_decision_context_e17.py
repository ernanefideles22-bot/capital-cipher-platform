"""E1.7 read-only root cause grouping, no free-text export."""
from datetime import datetime, timezone
from types import SimpleNamespace
from app.strategy.paper_decision_context_e17 import inspect_decision_context

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)

def decision(id, action="SELL", risk="BLOCKED", summary=None):
    return SimpleNamespace(
        id=id, candidate_action=action, risk_status=risk,
        agent_summary=summary if summary is not None else [],
    )

def check(id, reason, status="BLOCKED"):
    return SimpleNamespace(decision_id=id, risk_status=status, reason=reason)

def order(id):
    return SimpleNamespace(decision_id=id)

def test_categories_and_pending_mismatch_are_not_interpreted_as_profitability():
    rows=[
        decision("stop", summary=[{"status":"COMPLETED","signal":"SELL"}, {"status":"FAILED","signal":"BLOCK"}]),
        decision("late", risk="PENDING",summary=[{"status":"COMPLETED","signal":"SELL"}]),
        decision("good",action="BUY",risk="APPROVED"),
        decision("wait",action="WAIT",risk="PENDING",summary=[{"status":"COMPLETED","signal":"NEUTRAL"}]),
    ]
    risks=[
        check("stop","Open position limit reached; Consecutive loss limit reached"),
        check("late","Risk within configured portfolio limits",status="APPROVED"),
        check("good","Risk within configured portfolio limits",status="APPROVED"),
    ]
    result=inspect_decision_context(rows, risks, [order("good")])
    assert result["mode"] == "HISTORICAL_DIAGNOSTIC_ONLY"
    assert result["blocked_risk_reason_categories"] == {
        "CONSECUTIVE_LOSSES":1, "MAX_OPEN_POSITIONS":1,
    }
    assert result["directional_status_and_order_funnel"]["SELL"]["risk_status_mismatch"]==1
    assert result["directional_status_and_order_funnel"]["SELL"]["pending_with_risk_row"]==1
    assert result["directional_status_and_order_funnel"]["BUY"]["order_visible"]==1
    assert result["embedded_summary_coverage"] == {
        "decisions_with_embedded_agent_summary":3,
        "decisions_without_embedded_agent_summary":1,
    }
    assert result["embedded_summary_agent_statuses"]["COMPLETED"]==3
    assert "Open position" not in str(result)

def test_unknown_risk_reason_has_safe_category():
    result=inspect_decision_context(
        [decision("foo"), decision("bar",action="BUY",risk="PENDING")],
        [check("foo","Private secret: avoid including this text")],
        [],
    )
    assert result["blocked_risk_reason_categories"]=={"OTHER_OR_UNRECOGNIZED":1}
    assert "Private secret" not in str(result)
    assert result["directional_status_and_order_funnel"]["BUY"]["pending_without_risk_row"]==1

def test_kill_switch_and_no_risk_evidence():
    x=inspect_decision_context(
        [decision("kill",risk="KILL_SWITCH"),decision("none")],
        [check("kill","Kill switch active",status="KILL_SWITCH")],[],
    )
    assert x["blocked_risk_reason_categories"]=={
        "BLOCKED_WITHOUT_VISIBLE_RISK_ROW":1, "KILL_SWITCH":1,
    }

"""E1.4 research protocol: post-hoc cohort summaries cannot authorize trades."""
import copy

import pytest

from app.strategy.paper_shadow_cohorts_e14 import (
    FROZEN_PROTOCOL, describe_shadow_cohorts, protocol_hash,
)


def trade(oid, pnl):
    return {"order_id": oid, "realized_net": pnl}


def cost(oid, rr, status="EVALUATED"):
    return {
        "order_id": oid, "status": status,
        "hypothetical_net_reward_risk": rr,
    }


def bundle():
    report = {"trades": [
        trade("a", -10.0), trade("b", 15.0),
        trade("c", -20.0), trade("open", None),
    ]}
    baseline = [
        cost("a", 1.7), cost("b", 1.2),
        cost("c", 0.9), cost("open", 2.0),
    ]
    stress = [
        cost("a", 1.4), cost("b", 1.0),
        cost("c", 0.7), cost("open", 1.6),
    ]
    economics = {
        "mode": "SHADOW_RESEARCH_ONLY_NO_EXECUTION",
        "minimum_net_rr_for_illustrative_screen": 1.5,
        "scenarios": [
            {"scenario": "BASE_NO_VOLUME_IMPACT", "per_order_results": baseline},
            {"scenario": "STRESS_MAX_VOLUME_IMPACT", "per_order_results": stress},
        ],
    }
    return report, economics


def test_frozen_protocol_and_posthoc_outcomes_remain_descriptive():
    report, diagnostics = bundle()
    x = describe_shadow_cohorts(report, diagnostics)
    assert x["mode"] == "POST_HOC_IN_SAMPLE_DESCRIPTIVE_ONLY"
    assert not FROZEN_PROTOCOL["strategy_promotion_authorized"]
    assert not FROZEN_PROTOCOL["live_or_testnet_authorized"]
    assert x["prospective_readiness"] == "NOT_STARTED_AND_NOT_EVALUATED"
    assert x["no_profitability_or_causality_claim"]
    assert x["frozen_protocol_sha256"] == protocol_hash()
    a, b = x["scenarios"]
    assert a["group_counts"] == {
        "hypothetical_eligible": 2,
        "hypothetical_below_threshold": 2,
        "insufficient_pretrade_evidence": 0,
    }
    assert a["historically_observed_baseline"]["historically_closed"] == 3
    assert a["historically_observed_baseline"]["historically_open"] == 1
    assert a["historically_observed_baseline"]["historical_net_pnl"] == -15.0
    assert a["observed_eligible_subset"]["historical_net_pnl"] == -10.0
    assert a["observed_eligible_subset"]["historically_open"] == 1
    assert a["observed_below_threshold_subset"]["historical_net_pnl"] == -5.0
    assert b["observed_eligible_subset"]["historically_closed"] == 0
    assert b["observed_eligible_subset"]["historically_open"] == 1
    assert b["observed_eligible_subset"]["historical_profit_factor"] is None


def test_missing_cost_rows_duplicate_rows_and_protocol_drift_fail_closed():
    report, diagnostics = bundle()
    truncated = copy.deepcopy(diagnostics)
    truncated["scenarios"][0]["per_order_results"].pop()
    with pytest.raises(ValueError, match="incomplete forensic order coverage"):
        describe_shadow_cohorts(report, truncated)
    duplicate = copy.deepcopy(diagnostics)
    duplicate["scenarios"][0]["per_order_results"][0] = duplicate["scenarios"][0]["per_order_results"][1]
    with pytest.raises(ValueError, match="match exactly"):
        describe_shadow_cohorts(report, duplicate)
    drift = copy.deepcopy(diagnostics)
    drift["minimum_net_rr_for_illustrative_screen"] = 2.0
    with pytest.raises(ValueError, match="threshold"):
        describe_shadow_cohorts(report, drift)
    changed_scenarios = copy.deepcopy(diagnostics)
    changed_scenarios["scenarios"][0]["scenario"] = "LOOKAHEAD"
    with pytest.raises(ValueError, match="scenario"):
        describe_shadow_cohorts(report, changed_scenarios)


def test_no_closed_trades_does_not_manufacture_profitability():
    report, diagnostics = bundle()
    report["trades"] = [trade("open", None)]
    for s in diagnostics["scenarios"]:
        s["per_order_results"] = [cost("open", 1.6)]
    x = describe_shadow_cohorts(report, diagnostics)
    for row in x["scenarios"]:
        stats = row["observed_eligible_subset"]
        assert stats["historically_closed"] == 0
        assert stats["historical_win_rate"] is None
        assert stats["historical_profit_factor"] is None
        assert stats["historical_net_pnl"] == 0.0


def test_frozen_threshold_requires_explicit_cost_research_source():
    report, diagnostics = bundle()
    diagnostics["mode"] = "AUTOMATED_ORDER_GATE"
    with pytest.raises(ValueError, match="research-only"):
        describe_shadow_cohorts(report, diagnostics)

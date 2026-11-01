"""E1.3 isolated, synthetic net-risk diagnostics; no market or broker I/O."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.strategy.paper_net_economics_e13 import (
    BASE_COST, STRESS_COST, CostScenario, analyze_cost_diagnostics,
    planned_net_economics,
)


def order(oid="e13-1", *, side="BUY", stop=None, target=None, entry=100.0):
    return SimpleNamespace(
        id=oid, symbol="BTCUSDT", strategy="SCALP_15M_v1",
        side=side, status="CLOSED", entry_price=entry,
        position_size=1000.0,
        stop_loss=99.0 if stop is None and side == "BUY"
                  else 101.0 if stop is None else stop,
        take_profit=102.0 if target is None and side == "BUY"
                    else 98.0 if target is None else target,
        pnl=-9999.0, fees_estimated=2222.0,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_hypothetical_net_risk_uses_quote_notional_and_no_realized_costs(side):
    o = order(side=side)
    base = planned_net_economics(o, BASE_COST)
    stress = planned_net_economics(o, STRESS_COST)
    assert base["status"] == "EVALUATED"
    assert base["gross_planned_risk"] == 10.0
    assert base["gross_planned_reward_risk"] == 2.0
    assert base["hypothetical_net_at_stop"] < -10.0
    assert 0.0 < base["hypothetical_net_at_target"] < 20.0
    assert 1.5 < base["hypothetical_net_reward_risk"] < 2.0
    assert 1.0 < stress["hypothetical_net_reward_risk"] < 1.5
    assert stress["hypothetical_net_reward_risk"] < base["hypothetical_net_reward_risk"]
    assert stress["hypothetical_break_even_win_rate"] > base["hypothetical_break_even_win_rate"]
    # Closed realized net and fees must not enter hypothetical re-pricing.
    clean = replace_dummy(o, pnl=0.0, fees_estimated=0.0)
    assert planned_net_economics(clean, BASE_COST) == base


def replace_dummy(obj, **updates):
    return SimpleNamespace(**{**vars(obj), **updates})


def test_research_only_shadow_screen_counts_cost_sensitivity():
    result = analyze_cost_diagnostics([order("a"), order("b", side="SELL")])
    assert result["mode"] == "SHADOW_RESEARCH_ONLY_NO_EXECUTION"
    assert not result["assumptions_verified_against_staging_runtime"]
    base, stress = result["scenarios"]
    assert base["evaluable"] == stress["evaluable"] == 2
    assert base["hypothetical_meets_minimum_net_rr"] == 2
    assert base["hypothetical_fails_minimum_net_rr"] == 0
    assert stress["hypothetical_meets_minimum_net_rr"] == 0
    assert stress["hypothetical_fails_minimum_net_rr"] == 2
    assert all(x["status"] == "EVALUATED" for x in base["per_order_results"])


def test_small_stop_with_costs_can_make_positive_gross_rr_nonpositive():
    tiny = order("tiny", stop=99.99, target=100.02)
    result = planned_net_economics(tiny, STRESS_COST)
    assert result["gross_planned_reward_risk"] == 2.0
    assert result["hypothetical_net_at_target"] < 0.0
    assert result["hypothetical_net_reward_risk"] is None
    assert result["hypothetical_break_even_win_rate"] is None


def test_missing_and_invalid_orders_remain_unranked():
    no_target = replace_dummy(order("no_target"), take_profit=None)
    reversed_stop = order("wrong_stop", stop=101.0)
    nonfinite = order("nonfinite", entry=float("nan"))
    negative = order("negative", entry=-1.0)
    report = analyze_cost_diagnostics([no_target, reversed_stop, nonfinite, negative])
    for scenario in report["scenarios"]:
        assert scenario["evaluable"] == 0
        assert scenario["insufficient_evidence"] == 4
        assert scenario["hypothetical_meets_minimum_net_rr"] == 0
        assert scenario["hypothetical_fails_minimum_net_rr"] == 0


def test_open_positions_are_scenarios_not_realized_pnl():
    opened = replace_dummy(order("open"), status="FILLED", pnl=None)
    report = analyze_cost_diagnostics([opened])
    assert report["scenarios"][0]["evaluable"] == 1
    assert report["scenarios"][0]["per_order_results"][0]["status"] == "EVALUATED"


def test_no_orders_and_duplicate_orders():
    result = analyze_cost_diagnostics([])
    assert result["scenarios"][0]["evaluable"] == 0
    assert result["scenarios"][0]["hypothetical_net_rr_median"] is None
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_cost_diagnostics([order("same"), order("same")])


def test_invalid_assumptions_and_threshold_rejected():
    with pytest.raises(ValueError, match="nonnegative"):
        CostScenario(name="invalid", taker_fee_bps_per_side=-1)
    with pytest.raises(ValueError, match="positive"):
        analyze_cost_diagnostics([], minimum_net_rr=0)

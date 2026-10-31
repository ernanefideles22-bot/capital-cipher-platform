"""E1.1 integrity classification: diagnostics must not manufacture causality."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.strategy.paper_evidence_integrity_e11 import inspect_evidence


NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def order(id, *, side="SELL", status="CLOSED", pnl=-155.0):
    return SimpleNamespace(
        id=id, decision_id=f"decision-{id}", risk_check_id=f"risk-{id}",
        correlation_id=f"corr-{id}", symbol="ETHUSDT",
        timeframe="15m", strategy="SCALP_15M_v1", side=side,
        status=status, entry_price=100.0, stop_loss=105.0 if side == "SELL" else 95.0,
        position_size=1000.0, pnl=pnl, fees_estimated=5.0,
        created_at=NOW, opened_at=NOW,
    )


def test_missing_row_must_not_be_called_missing_approval():
    o = order("a")
    result = inspect_evidence(
        [o], {}, {}, {},
        any_decision_visible=False, any_risk_visible=False,
    )
    assert result["decision_link_status"] == {"NOT_VISIBLE_BY_ID": 1}
    assert result["risk_link_status"] == {"NOT_VISIBLE_BY_ID": 1}
    assert not result["decision_table_has_visible_rows"]
    assert "RLS" in result["table_visibility_meaning"]
    assert result["stop_execution"]["worst_five_stop_closures"] == []


def test_stop_overshoot_and_recorded_fees_not_double_subtracted():
    o = order("worst", side="SELL", pnl=-155.0)
    d = SimpleNamespace(
        correlation_id=o.correlation_id,
        symbol=o.symbol, timeframe=o.timeframe,
        created_at=NOW - timedelta(seconds=3),
    )
    r = SimpleNamespace(
        decision_id=o.decision_id, correlation_id=o.correlation_id,
        created_at=NOW - timedelta(seconds=2), approved=True,
    )
    closure = SimpleNamespace(payload={"exit_reason": "STOP_LOSS", "exit_price": 115.0})
    result = inspect_evidence(
        [o], {o.decision_id: d}, {o.risk_check_id: r},
        {o.id: closure},
        any_decision_visible=True, any_risk_visible=True,
    )
    assert result["decision_link_status"] == {"VERIFIED": 1}
    assert result["risk_link_status"] == {"VERIFIED": 1}
    stop = result["stop_execution"]
    assert stop["recorded_stop_closures"] == 1
    assert stop["stops_with_price_and_risk_evidence"] == 1
    assert stop["exit_price_worse_than_stop"] == 1
    assert stop["net_loss_below_minus_1r"] == 1
    worst = stop["worst_five_stop_closures"][0]
    # 1000 notional / 100 entry = 10 ETH, 115 is 15 adverse.
    # Gross -150, one total 5 fee, realized -155 (3.1R).
    assert worst["gross_pnl_from_fill"] == -150.0
    assert worst["net_costs_inferred"] == 5.0
    assert worst["recorded_total_fees"] == 5.0
    assert worst["non_fee_costs_or_rounding"] == 0.0
    assert worst["realized_r"] == -3.1
    assert worst["adverse_exit_gap_r"] == 2.0


def test_visible_rows_with_wrong_correlation_are_not_verified():
    o = order("mismatch")
    d = SimpleNamespace(
        correlation_id="other", symbol=o.symbol, timeframe=o.timeframe,
        created_at=NOW - timedelta(seconds=1),
    )
    r = SimpleNamespace(
        decision_id=o.decision_id, correlation_id="other",
        created_at=NOW - timedelta(seconds=1), approved=True,
    )
    result = inspect_evidence(
        [o], {o.decision_id: d}, {o.risk_check_id: r}, {},
        any_decision_visible=True, any_risk_visible=True,
    )
    assert result["decision_link_status"] == {"CORRELATION_MISMATCH": 1}
    assert result["risk_link_status"] == {"CORRELATION_MISMATCH": 1}


def test_unapproved_risk_never_reported_verified():
    o = order("unapproved")
    r = SimpleNamespace(
        decision_id=o.decision_id, correlation_id=o.correlation_id,
        created_at=NOW - timedelta(seconds=1), approved=False,
    )
    result = inspect_evidence(
        [o], {}, {o.risk_check_id: r}, {},
        any_decision_visible=True, any_risk_visible=True,
    )
    assert result["risk_link_status"] == {"REJECTED_OR_UNAPPROVED": 1}

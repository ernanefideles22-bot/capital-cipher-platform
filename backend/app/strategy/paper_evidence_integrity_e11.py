"""E1.1: categorize forensic evidence visibility and realized stop execution.

Pure analysis. No credentials, orders, adapters, writes or exchange actions.
"Not visible" must NEVER be described as "never existed": RLS, missing
historical partitions and retention can produce identical SELECT results.
"""
from __future__ import annotations

from collections import Counter
from math import isfinite


def _decision_status(order, decision) -> str:
    if decision is None:
        return "NOT_VISIBLE_BY_ID"
    if decision.correlation_id != order.correlation_id:
        return "CORRELATION_MISMATCH"
    if decision.symbol != order.symbol:
        return "SYMBOL_MISMATCH"
    if decision.timeframe != order.timeframe:
        return "TIMEFRAME_MISMATCH"
    if decision.created_at > order.created_at:
        return "CREATED_AFTER_ENTRY"
    return "VERIFIED"


def _risk_status(order, risk) -> str:
    if risk is None:
        return "NOT_VISIBLE_BY_ID"
    if risk.decision_id != order.decision_id:
        return "DECISION_ID_MISMATCH"
    if risk.correlation_id != order.correlation_id:
        return "CORRELATION_MISMATCH"
    if risk.created_at > order.created_at:
        return "CREATED_AFTER_ENTRY"
    if not risk.approved:
        return "REJECTED_OR_UNAPPROVED"
    return "VERIFIED"


def inspect_evidence(
    orders, decisions: dict, risks: dict, audit_by_order: dict,
    *, any_decision_visible: bool, any_risk_visible: bool,
) -> dict:
    """Read-only explanation of what was and wasn't visible in the E1 snapshot.

    Orders, decisions and risks are ORM rows from the SAME READ ONLY session.
    Global visibility means SELECT(id).limit(1) returned a row under THIS role,
    not that the history is complete or that RLS did not filter another row.
    """
    decision_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    worst_stops = []
    stop_counts: Counter[str] = Counter()
    market_clock_lags: list[float] = []
    for order in orders:
        if order.opened_at is not None:
            lag = (order.created_at - order.opened_at).total_seconds()
            if isfinite(lag):
                market_clock_lags.append(lag)
        decision_counts[_decision_status(order, decisions.get(order.decision_id))] += 1
        risk_counts[_risk_status(order, risks.get(order.risk_check_id))] += 1
        if order.status != "CLOSED" or order.pnl is None:
            continue
        audit = audit_by_order.get(order.id)
        payload = audit.payload if audit is not None and isinstance(audit.payload, dict) else {}
        if payload.get("exit_reason") != "STOP_LOSS":
            continue
        stop_counts["recorded_stop_closures"] += 1
        exit_price = payload.get("exit_price")
        if exit_price is None or order.stop_loss is None or order.entry_price is None:
            stop_counts["no_exit_or_stop_evidence"] += 1
            continue
        entry = float(order.entry_price)
        stop = float(order.stop_loss)
        exit_value = float(exit_price)
        notional = float(order.position_size)
        net = float(order.pnl)
        fees = float(order.fees_estimated or 0.0)
        direction = 1 if order.side == "BUY" else -1
        distance = (entry - stop) * direction
        values = (entry, stop, exit_value, notional, net, fees)
        if (not all(isfinite(v) for v in values) or entry <= 0
                or notional <= 0 or distance <= 0):
            stop_counts["invalid_price_or_risk_evidence"] += 1
            continue
        risk_budget = notional * distance / entry
        quantity = notional / entry
        gross = (exit_value - entry) * quantity * direction
        realized_r = net / risk_budget
        # Positive means execution happened adversely beyond planned stop;
        # it can reflect candle gaps, fill modeling, or order slippage.
        adverse_gap_r = max(0.0, (stop - exit_value) * direction / distance)
        inferred_cost = gross - net  # fee + funding + liquidation etc.
        cost_other_than_recorded_fees = inferred_cost - fees
        stop_counts["stops_with_price_and_risk_evidence"] += 1
        if realized_r < -1:
            stop_counts["net_loss_below_minus_1r"] += 1
        if adverse_gap_r > 1e-7:
            stop_counts["exit_price_worse_than_stop"] += 1
        worst_stops.append({
            "order_id": order.id,
            "symbol": order.symbol,
            "strategy": order.strategy,
            "side": order.side,
            "net_pnl": round(net, 6),
            "realized_r": round(realized_r, 6),
            "adverse_exit_gap_r": round(adverse_gap_r, 6),
            "recorded_total_fees": round(fees, 6),
            "gross_pnl_from_fill": round(gross, 6),
            "net_costs_inferred": round(inferred_cost, 6),
            "non_fee_costs_or_rounding": round(cost_other_than_recorded_fees, 6),
        })
    worst_stops.sort(key=lambda row: row["realized_r"])
    return {
        "closed_and_open_orders_examined": len(orders),
        "decision_link_status": dict(sorted(decision_counts.items())),
        "risk_link_status": dict(sorted(risk_counts.items())),
        "market_clock_vs_processing_clock": {
            "orders_with_two_timestamps": len(market_clock_lags),
            "orders_with_market_timestamp_before_creation":
                sum(x > 0.001 for x in market_clock_lags),
            "max_market_to_order_creation_lag_seconds":
                round(max(market_clock_lags), 6) if market_clock_lags else None,
            "min_market_to_order_creation_lag_seconds":
                round(min(market_clock_lags), 6) if market_clock_lags else None,
            "interpretation": (
                "opened_at is market candle closed_at, not persisted execution "
                "creation time. A positive lag does not prove the historical "
                "candle close was an executable fill when the order was created."
            ),
        },
        "decision_table_has_visible_rows": any_decision_visible,
        "risk_table_has_visible_rows": any_risk_visible,
        "table_visibility_meaning": (
            "SELECT visibility under auditor role only; empty may mean RLS, "
            "retention, missing historical data, or truly empty table."
        ),
        "stop_execution": {
            **dict(sorted(stop_counts.items())),
            "worst_five_stop_closures": worst_stops[:5],
            "caveat": (
                "Adverse stop gap measures executed fill against planned stop; "
                "it does NOT establish cause without point-in-time OHLCV, "
                "spread and simulated fill assumptions. Net P&L already "
                "includes fees; never subtract them twice."
            ),
        },
    }

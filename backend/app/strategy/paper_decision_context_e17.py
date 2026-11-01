"""E1.7: categorize risk-block reasons and embedded decision agent evidence.

Read-only aggregation only. Never export free-text reasons, agent names or
per-candidate identifiers; unknown reasons remain UNKNOWN, not invented causes.
"""
from __future__ import annotations

from collections import Counter


def _risk_reasons(reason) -> set[str]:
    if not isinstance(reason, str) or not reason.strip():
        return {"EMPTY_REASON"}
    rules = (
        ("KILL_SWITCH", "kill switch"),
        ("SYSTEM_STATE", "system state"),
        ("DATA_QUALITY", "data quality score"),
        ("STALE_MARKET_DATA", "market data delay"),
        ("DAILY_DRAWDOWN", "daily drawdown"),
        ("TOTAL_DRAWDOWN", "total drawdown"),
        ("CONSECUTIVE_LOSSES", "consecutive loss"),
        ("MAX_OPEN_POSITIONS", "open position limit"),
        ("LEVERAGE", "leverage "),
        ("PORTFOLIO_CONSTRUCTION", "portfolio construction"),
        ("PORTFOLIO_EXPOSURE", "portfolio exposure"),
        ("SYMBOL_CONCENTRATION", "symbol concentration"),
        ("PORTFOLIO_VAR", "portfolio var"),
        ("GROSS_REWARD_RISK", "risk/reward "),
        ("UNEXECUTABLE_ACTION", "not executable"),
    )
    components = reason.lower().split(";")
    output = set()
    for part in components:
        found = [name for name, pattern in rules if pattern in part]
        output.update(found or ["OTHER_OR_UNRECOGNIZED"])
    return output


def inspect_decision_context(decisions, risks, orders) -> dict:
    risk_by_decision = {}
    for r in risks:
        risk_by_decision.setdefault(r.decision_id, []).append(r)
    order_by_decision = {}
    for o in orders:
        order_by_decision.setdefault(o.decision_id, []).append(o)

    summary_coverage = Counter()
    summary_statuses = Counter()
    summary_signals = Counter()
    risk_reason_categories = Counter()
    block_reason_by_action = {}
    funnel = {}
    for d in decisions:
        action = str(d.candidate_action or "UNKNOWN")
        summary = getattr(d, "agent_summary", None)
        if isinstance(summary, list) and len(summary):
            summary_coverage["decisions_with_embedded_agent_summary"] += 1
            for item in summary[:500]:
                if not isinstance(item, dict):
                    summary_statuses["INVALID_ENTRY"] += 1
                    continue
                summary_statuses[str(item.get("status") or "UNKNOWN")] += 1
                summary_signals[str(item.get("signal") or "UNKNOWN")] += 1
            if len(summary) > 500:
                summary_coverage["decisions_summary_entry_truncated"] += 1
        else:
            summary_coverage["decisions_without_embedded_agent_summary"] += 1
        risk_rows = risk_by_decision.get(d.id, [])
        order_rows = order_by_decision.get(d.id, [])
        if action not in {"BUY", "SELL"}:
            continue
        status = str(d.risk_status or "UNKNOWN")
        row = funnel.setdefault(action, Counter())
        row[f"decision_status_{status}"] += 1
        row["order_visible" if order_rows else "no_order_visible"] += 1
        if not risk_rows:
            row["no_risk_row_visible"] += 1
        else:
            row["risk_row_visible"] += 1
            matching = any(str(r.risk_status) == status for r in risk_rows)
            row["risk_status_matches_decision" if matching
                else "risk_status_mismatch"] += 1
        if status == "PENDING":
            row["pending_with_order" if order_rows
                else "pending_without_order"] += 1
            row["pending_with_risk_row" if risk_rows
                else "pending_without_risk_row"] += 1
        if status not in {"BLOCKED", "KILL_SWITCH"}:
            continue
        if not risk_rows:
            risk_reason_categories["BLOCKED_WITHOUT_VISIBLE_RISK_ROW"] += 1
            continue
        codes = set()
        for r in risk_rows:
            if str(r.risk_status) in {"BLOCKED", "KILL_SWITCH"}:
                codes.update(_risk_reasons(getattr(r, "reason", None)))
        if not codes:
            codes.add("BLOCKED_BUT_NO_BLOCKED_RISK_ROW")
        for code in codes:
            risk_reason_categories[code] += 1
            block_reason_by_action.setdefault(action, Counter())[code] += 1
    return {
        "mode": "HISTORICAL_DIAGNOSTIC_ONLY",
        "embedded_summary_coverage": dict(sorted(summary_coverage.items())),
        "embedded_summary_agent_statuses": dict(sorted(summary_statuses.items())),
        "embedded_summary_agent_signals": dict(sorted(summary_signals.items())),
        "directional_status_and_order_funnel": {
            key: dict(sorted(values.items())) for key, values in sorted(funnel.items())
        },
        "blocked_risk_reason_categories": dict(sorted(risk_reason_categories.items())),
        "blocked_risk_categories_by_action": {
            key: dict(sorted(values.items()))
            for key, values in sorted(block_reason_by_action.items())
        },
        "limitations": [
            "One decision may have multiple distinct risk block reasons.",
            "Reason codes are sanitized categories, not verbatim risk messages.",
            "An embedded summary is evidence of serialized opinions, not proof of fresh executions.",
            "A pending status with a risk row may reflect asynchronous persistence or divergent snapshots.",
            "No OOS, profitability, strategy promotion or operational authorization is inferred.",
        ],
    }

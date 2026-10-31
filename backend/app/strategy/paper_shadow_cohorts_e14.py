"""E1.4 frozen SHADOW-only cohort descriptions, not a trading gate.

All historical outcome comparisons are post-hoc and IN SAMPLE. They cannot
establish a causal improvement, a strategy edge or an out-of-sample result.
No OMS, risk, exchange, learning weights or state changes exist here.
"""
from __future__ import annotations

import hashlib
import json
from math import isfinite

FROZEN_PROTOCOL = {
    "protocol": "E1.4_SHADOW_NET_RR_V1",
    "mode": "RESEARCH_ONLY_NO_EXECUTION",
    "threshold_net_rr": 1.5,
    "scenarios": ["BASE_NO_VOLUME_IMPACT", "STRESS_MAX_VOLUME_IMPACT"],
    "prospective_not_before_utc": "2026-11-02T00:00:00+00:00",
    "required_closed_prospective": 100,
    "required_distinct_trading_days": 30,
    "archive_reconciliation_required": True,
    "strategy_promotion_authorized": False,
    "live_or_testnet_authorized": False,
}


def protocol_hash() -> str:
    return hashlib.sha256(
        json.dumps(FROZEN_PROTOCOL, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _aggregate_outcomes(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["realized_net"] is not None]
    pnls = [float(t["realized_net"]) for t in closed]
    if not all(isfinite(pnl) for pnl in pnls):
        raise ValueError("Nonfinite realized PAPER PnL in E1.4")
    gain = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    return {
        "historically_closed": len(closed),
        "historically_open": len(trades) - len(closed),
        "historical_wins": sum(p > 0 for p in pnls),
        "historical_net_pnl": round(sum(pnls), 6),
        "historical_profit_factor": (
            round(gain / loss, 6) if loss > 0 else None
        ),
        "profit_factor_status": (
            "NO_LOSSES_UNDEFINED" if loss == 0 else "FINITE"
        ),
        "historical_win_rate": (
            round(sum(p > 0 for p in pnls) / len(pnls), 6)
            if pnls else None
        ),
    }


def describe_shadow_cohorts(report: dict, cost_diagnostics: dict) -> dict:
    """Compare observed outcomes in post-hoc filtered subsets, never simulate blocking.

    Uses all selected pre-trade fields in E1.3, but closes are only observed
    outcomes of orders the system ACTUALLY executed. No alternative portfolio,
    path-dependent sizing, rejected trade, agent or fill outcome is inferred.
    """
    trades = report["trades"]
    by_id: dict[str, dict] = {}
    for row in trades:
        oid = str(row["order_id"])
        if oid in by_id:
            raise ValueError("E1.4 duplicate forensic order")
        by_id[oid] = row
    if cost_diagnostics.get("mode") != "SHADOW_RESEARCH_ONLY_NO_EXECUTION":
        raise ValueError("E1.4 requires research-only cost diagnostics")
    thresh = float(FROZEN_PROTOCOL["threshold_net_rr"])
    if cost_diagnostics.get("minimum_net_rr_for_illustrative_screen") != thresh:
        raise ValueError("E1.4 frozen threshold mismatch")
    scenarios = cost_diagnostics["scenarios"]
    if [s["scenario"] for s in scenarios] != FROZEN_PROTOCOL["scenarios"]:
        raise ValueError("E1.4 scenario names/order mismatch")
    outcome = []
    for scenario in scenarios:
        eligible: list[dict] = []
        ineligible: list[dict] = []
        incomplete: list[dict] = []
        seen = set()
        for x in scenario["per_order_results"]:
            oid = str(x["order_id"])
            if oid not in by_id or oid in seen:
                raise ValueError("E1.4 cost row must match exactly one forensic order")
            seen.add(oid)
            t = by_id[oid]
            if x["status"] != "EVALUATED":
                incomplete.append(t)
                continue
            rr = x.get("hypothetical_net_reward_risk")
            if rr is not None and (not isinstance(rr, (int, float)) or not isfinite(rr)):
                raise ValueError("Invalid net RR")
            if rr is not None and rr >= thresh:
                eligible.append(t)
            else:
                ineligible.append(t)
        if seen != set(by_id):
            raise ValueError("E1.4 incomplete forensic order coverage")
        outcome.append({
            "scenario": scenario["scenario"],
            "group_counts": {
                "hypothetical_eligible": len(eligible),
                "hypothetical_below_threshold": len(ineligible),
                "insufficient_pretrade_evidence": len(incomplete),
            },
            "historically_observed_baseline": _aggregate_outcomes(trades),
            "observed_eligible_subset": _aggregate_outcomes(eligible),
            "observed_below_threshold_subset": _aggregate_outcomes(ineligible),
            "observed_insufficient_subset": _aggregate_outcomes(incomplete),
        })
    return {
        "mode": "POST_HOC_IN_SAMPLE_DESCRIPTIVE_ONLY",
        "frozen_protocol": FROZEN_PROTOCOL,
        "frozen_protocol_sha256": protocol_hash(),
        "scenarios": outcome,
        "prospective_readiness": "NOT_STARTED_AND_NOT_EVALUATED",
        "no_profitability_or_causality_claim": True,
        "caveats": [
            "These are historical subsets, NOT profits that would have been realized by blocking orders.",
            "Actual entries, capital reuse, open positions and overlap change under a real gate.",
            "Cost assumptions are unverified; there is no point-in-time executable quote.",
            "This dataset was available before protocol freeze and is IN SAMPLE.",
            "Do not enable filters, modify weights or judge OOS without separate prospective collection.",
        ],
    }

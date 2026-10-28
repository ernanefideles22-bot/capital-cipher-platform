"""Offline research review of private PAPER evidence; no execution authority.

Consumes sealed snapshots. Does not connect to the runtime or modify its
candidate classification, strategies, limits, weights, approvals or orders.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "forward_observer", Path(__file__).with_name("observe_paper_forward.py")
)
observer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(observer)


def wilson_interval(accuracy: float | None, samples: int) -> list[float] | None:
    """Descriptive 95% Wilson interval, without multiple-selection correction."""
    if type(samples) is not int or samples < 0:
        raise ValueError("Invalid directional sample count")
    if samples == 0:
        if accuracy is not None:
            raise ValueError("Accuracy without directional observations")
        return None
    if not observer.number(accuracy) or not 0 <= accuracy <= 1:
        raise ValueError("Invalid directional accuracy")
    z = 1.959963984540054
    denominator = 1 + z * z / samples
    center = (accuracy + z * z / (2 * samples)) / denominator
    half_width = z * math.sqrt(
        accuracy * (1 - accuracy) / samples + z * z / (4 * samples * samples)
    ) / denominator
    return [round(max(0, center - half_width), 8), round(min(1, center + half_width), 8)]


def agent_review(data: dict, minimum_directional: int = 30) -> list[dict]:
    if type(minimum_directional) is not int or minimum_directional < 1:
        raise ValueError("Invalid research minimum")
    candidates = {
        (row["agent_name"], row["agent_version"])
        for row in data["candidates"]["candidates"]
        if row.get("eligible_for_regime_shadow_test") is True
    }
    result = []
    for card in data["scorecards"]["scorecards"]:
        total, directional = card["sample_count"], card["directional_sample_count"]
        if type(total) is not int or total < 0 or type(directional) is not int or not 0 <= directional <= total:
            raise ValueError("Invalid sample accounting")
        interval = wilson_interval(card["accuracy"], directional)
        result.append({
            "agent_name": card["agent_name"], "agent_version": card["agent_version"],
            "total_samples": total, "directional_samples": directional,
            "directional_coverage_percent": round(directional / total * 100, 4) if total else None,
            "accuracy": card["accuracy"], "wilson_95_descriptive": interval,
            "runtime_candidate": (card["agent_name"], card["agent_version"]) in candidates,
            "research_minimum_directional_met": directional >= minimum_directional,
            "descriptive_lower_bound_above_half": interval is not None and interval[0] > 0.5,
            "mean_brier_loss": card.get("mean_brier_loss"),
            "mean_marginal_contribution": card.get("mean_marginal_contribution"),
        })
    return sorted(result, key=lambda row: (not row["runtime_candidate"], row["agent_name"]))


def book_review(data: dict) -> dict:
    """Compute actual open notional; do not net opposing sides out of gross."""
    orders = data["orders"]["orders"]
    seen = set()
    gross, net = 0.0, 0.0
    by_symbol, by_strategy, by_timeframe, sides = {}, {}, {}, {}
    count = 0
    for order in orders:
        identity = order["paper_order_id"]
        if identity in seen:
            raise ValueError("Duplicate order identity")
        seen.add(identity)
        if order.get("closed_at") is not None:
            if order["status"] != "CLOSED":
                raise ValueError("Inconsistent closed order")
            continue
        if order["status"] != "FILLED":
            raise ValueError("Unresolved or partially filled order needs separate reconciliation")
        notional = order["position_size"]
        if not observer.number(notional) or notional <= 0 or order["side"] not in ("BUY", "SELL"):
            raise ValueError("Invalid open exposure")
        gross += notional
        net += notional if order["side"] == "BUY" else -notional
        count += 1
        for target, key in ((by_symbol, "symbol"), (by_strategy, "strategy"), (by_timeframe, "timeframe")):
            label = order.get(key)
            if not isinstance(label, str) or not label:
                raise ValueError("Unattributed open exposure")
            target[label] = round(target.get(label, 0) + notional, 8)
        sides.setdefault(order["symbol"], set()).add(order["side"])
    if count != data["performance"]["open_trades"]:
        raise ValueError("Order list does not reconcile with open trade count")
    return {"open_positions": count, "gross_notional": round(gross, 8),
            "net_notional": round(net, 8), "by_symbol": by_symbol,
            "by_strategy": by_strategy, "by_timeframe": by_timeframe,
            "mixed_side_symbols": sorted(k for k, values in sides.items() if len(values) > 1),
            "capital_policy": "one shared portfolio; no independent balance assigned to each timeframe"}


def review(snapshot: dict, risk_context: dict | None = None) -> dict:
    if snapshot.get("sha256") != observer.seal(snapshot)["sha256"]:
        raise ValueError("Snapshot integrity mismatch")
    if (snapshot.get("status") != "OBSERVING"
            or not observer.paper_scope(snapshot["data"]["status"], observer.EXPECTED_REVISION)):
        raise ValueError("Usable certified PAPER evidence required")
    data = snapshot["data"]
    book = book_review(data)
    agents = agent_review(data)
    runtime_candidates = [row for row in agents if row["runtime_candidate"]]
    risk = None
    if risk_context is not None:
        if (risk_context.get("schema") != "paper-risk-review-context-v1"
                or risk_context.get("sha256") != observer.seal(risk_context)["sha256"]
                or risk_context.get("origin") != snapshot["origin"]
                or risk_context.get("stable_runtime") is not True
                or risk_context.get("status_before") != risk_context.get("status_after")
                or not observer.paper_scope(risk_context["status_before"], observer.EXPECTED_REVISION)):
            raise ValueError("Unusable risk context")
        interval = abs((datetime.fromisoformat(risk_context["completed_at"])
                        - datetime.fromisoformat(snapshot["completed_at"])).total_seconds())
        if interval > 300:
            raise ValueError("Risk context is more than five minutes from the PAPER capture")
        portfolio = risk_context["data"]["risk"]["portfolio"]
        if (portfolio["position_count"] != book["open_positions"]
                or abs(portfolio["gross_exposure"] - book["gross_notional"]) > 1e-6
                or abs(portfolio["net_exposure"] - book["net_notional"]) > 1e-6):
            raise ValueError("Risk and PAPER books do not reconcile")
        risk = {"context_sha256": risk_context["sha256"],
                "limits_as_reported": risk_context["data"]["limits"],
                "portfolio_as_reported": portfolio,
                "consistency_scope": "sequential reports, not an atomic reservation or permission to enter"}
    return observer.seal({
        "schema": "paper-phase-c-research-review-v1", "created_at": observer.utcnow(),
        "snapshot_sha256": snapshot["sha256"], "source_revision": observer.EXPECTED_REVISION,
        "decision_authority": False, "automatic_weight_adjustment": False,
        "phase_c_certified": False,
        "performance_as_reported": data["performance"],
        "campaign_comparison": snapshot.get("comparison"),
        "timeframe_history": data["timeframes"]["breakdown"],
        "portfolio": book, "risk_context": risk,
        "agent_summary": {
            "scorecards": len(agents), "runtime_candidates": len(runtime_candidates),
            "candidates_below_30_directional": sum(not row["research_minimum_directional_met"] for row in runtime_candidates),
            "qualified_regime_specialists_as_reported": data["regimes"]["shadow_specialist_count"],
            "unavailable_historical_forecasts": data["regimes"]["unavailable_historical_forecasts"],
        },
        "agents": agents,
        "execution_timing": {"cycle_latency_ms": data["orchestrator"].get("cycle_latency_ms"),
                             "order_latency_ms": None,
                             "reason": "event/queue/decision/risk/submit/fill milestones not supplied"},
        "research_policy": {"minimum_directional_samples": 30,
                            "scope": "proposed research screen only; runtime eligibility is unchanged"},
        "limitations": [
            "Scorecards are cumulative, not necessarily outcomes from this campaign.",
            "Wilson intervals are descriptive: dependent forecasts and selection among 300 agents prevent a significance claim.",
            "A directional sample screen is not a qualified regime specialist, causal PnL or promotion approval.",
            "Open book excludes pending durable approvals and reservations; zero open exposure does not prove atomic capacity.",
            "Risk amounts are observed limits, not available cash or trade sizing instructions.",
            "Phase C still needs simultaneous three-desk, stale-signal, durable reservation and fault/restart evidence.",
        ],
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--risk-context", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import json
    context = json.loads(args.risk_context.read_text(), parse_constant=observer.reject_constant) if args.risk_context else None
    result = review(observer.load_snapshot(args.snapshot), context)
    observer.write_snapshot(args.output, result)
    print(json.dumps({"agent_summary": result["agent_summary"], "portfolio": result["portfolio"],
                      "phase_c_certified": False, "sha256": result["sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

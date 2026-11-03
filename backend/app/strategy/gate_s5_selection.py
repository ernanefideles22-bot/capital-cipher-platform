"""S5 descriptive selection foundation: review queue, never an execution gate.

Uses only the verified S4 lab journal. Current S3 accounts do not evaluate S4
mutations, so their results must never be inherited by a new candidate.
"""
from __future__ import annotations

from math import isfinite

from app.strategy.gate_s4_lab import read_campaign

POLICY = {
    "policy_id": "S5_DEVELOPMENT_REVIEW_V1",
    "min_closed_trades_each_validation_scenario": 30,
    "min_profit_factor": 1.2,
    "max_realized_drawdown_pct": 5.,
    "max_largest_win_share": .5,
}


def _screen(outcome):
    if "error_code" in outcome:
        return "EVALUATION_FAILED", [outcome["error_code"]], None
    insufficient, rejected, scores = [], [], []
    for fold in outcome["folds"]:
        label = f"FOLD_{fold['fold']}"
        for scenario in ("validation", "stress", "stress_delayed"):
            m = fold[scenario]
            key = label + "_" + scenario.upper()
            numbers = (m["closed_net_pnl"], m["realized_drawdown_pct"],
                       m["closed_fees"], m["closed_funding"])
            if not all(type(v) in (int, float) and isfinite(v) for v in numbers):
                raise ValueError("Nonfinite selection evidence")
            if m["completed_trades"] < POLICY["min_closed_trades_each_validation_scenario"]:
                insufficient.append(key + "_SAMPLE")
            if m["profit_factor"] is None or m["largest_win_share"] is None:
                insufficient.append(key + "_UNDEFINED_DISTRIBUTION")
            else:
                if not all(isfinite(v) for v in (m["profit_factor"], m["largest_win_share"])):
                    raise ValueError("Nonfinite distribution evidence")
                if m["profit_factor"] < POLICY["min_profit_factor"]:
                    rejected.append(key + "_PROFIT_FACTOR")
                if m["largest_win_share"] > POLICY["max_largest_win_share"]:
                    rejected.append(key + "_CONCENTRATED_WIN")
            if m["closed_net_pnl"] <= 0:
                rejected.append(key + "_NOT_ABOVE_CASH")
            if m["realized_drawdown_pct"] > POLICY["max_realized_drawdown_pct"]:
                rejected.append(key + "_DRAWDOWN")
            scores.append(m["closed_net_pnl"])
        if fold["validation"]["closed_net_pnl"] <= fold["parent_control"]["closed_net_pnl"]:
            rejected.append(label + "_NO_INCREMENTAL_NET_BENEFIT")
    # Do not turn tiny samples into confident negative/positive conclusions.
    status = ("INSUFFICIENT_DEVELOPMENT_EVIDENCE" if insufficient else
              "REJECTED_DEVELOPMENT_SCREEN" if rejected else
              "DESCRIPTIVE_REVIEW_CANDIDATE")
    return status, insufficient + rejected, min(scores) if scores else None


def selection_report(root):
    campaign = read_campaign(root)
    proposals = campaign["manifest"]["hypotheses"]
    from app.strategy.gate_s4_lab import Hypothesis
    outcomes = {value["candidate_id"]: value for value in campaign["outcomes"]}
    rows = []
    for proposal in proposals:
        candidate = Hypothesis(**proposal)
        outcome = outcomes.get(candidate.candidate_id)
        if outcome is None:
            status, reasons, score = "PENDING_EVALUATION", ["NO_RECORDED_RESULT"], None
        else:
            status, reasons, score = _screen(outcome)
        rankable = (status == "DESCRIPTIVE_REVIEW_CANDIDATE" and campaign["complete"]
                    and campaign["failed_candidates"] == 0)
        if not campaign["complete"]:
            reasons.append("CAMPAIGN_INCOMPLETE")
        if campaign["failed_candidates"]:
            reasons.append("CAMPAIGN_HAS_FAILED_TRIALS")
        rows.append({"candidate_id": candidate.candidate_id, "hypothesis": proposal,
                     "status": status, "reasons": reasons, "rankable_for_review": rankable,
                     "worst_validation_scenario_closed_net": score,
                     "rank": None, "execution_authorized": False,
                     "automatic_promotion_authorized": False})
    rankable = sorted((r for r in rows if r["rankable_for_review"]),
                      key=lambda r: (-r["worst_validation_scenario_closed_net"], r["candidate_id"]))
    for rank, row in enumerate(rankable, 1):
        row["rank"] = rank
    return {"model": "S5_OFFLINE_DESCRIPTIVE_SELECTION_V1", "policy": dict(POLICY),
            "source_journal_head": campaign["journal_head"],
            "registered_trials_including_failed": campaign["registered_candidates"],
            "completed_trials": len(campaign["outcomes"]),
            "failed_trials": campaign["failed_candidates"],
            "campaign_complete": campaign["complete"], "candidates": rows,
            "ranking_basis": "WORST_FOLD_AND_SCENARIO_CLOSED_NET_NO_ACCOUNT_POOLING",
            "development_validation_reused_for_screening": True,
            "holdout_evaluated": False, "multiple_testing_correction_completed": False,
            "cross_campaign_trials_accounted_for": False,
            "statistical_significance_established": False,
            "prospective_s4_candidate_evidence_available": False,
            "current_s3_results_transferable_to_s4_candidates": False,
            "profitability_proven": False, "execution_authorized": False,
            "automatic_promotion_authorized": False,
            "remaining_gates": ["INDEPENDENT_MULTIPLE_TESTING_REVIEW",
                                "UNTOUCHED_FINAL_HOLDOUT_WITH_FROZEN_RULES",
                                "SEPARATE_PROSPECTIVE_CANDIDATE_TRACKING",
                                "EXECUTION_AND_RISK_RECONCILIATION",
                                "EXPLICIT_OWNER_ORDER_AUTHORIZATION"]}

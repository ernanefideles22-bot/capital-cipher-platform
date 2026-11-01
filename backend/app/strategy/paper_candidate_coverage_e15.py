"""E1.5 candidate visibility preflight, READ ONLY and historical.

This is not a prospective collector. A bounded sample of persisted decisions
cannot prove that all market opportunities were ever persisted; a rejected
decision may produce no risk check by design. No trading, scoring or writes.
"""
from collections import Counter
from datetime import datetime, timezone


def describe_candidate_visibility(
    decisions, risks, paper_orders, agents, *, sampled_limit: int,
    has_more_decisions: bool, execution_jobs=(),
) -> dict:
    if sampled_limit < 1:
        raise ValueError("Invalid bounded decision sample")
    if len(decisions) > sampled_limit:
        raise ValueError("Unbounded decision sample")
    ids = [x.id for x in decisions]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate decision ID in snapshot")
    risk_map = {}
    for r in risks:
        risk_map.setdefault(r.decision_id, []).append(r)
    order_map = {}
    for o in paper_orders:
        order_map.setdefault(o.decision_id, []).append(o)
    agent_map = {}
    for a in agents:
        agent_map.setdefault(a.correlation_id, []).append(a)
    job_map = {}
    for job in execution_jobs:
        job_map.setdefault(job.correlation_id, []).append(job)
    actions = Counter()
    decision_risk_status = Counter()
    risk_coverage = Counter()
    order_coverage = Counter()
    agent_coverage = Counter()
    job_coverage = Counter()
    action_funnel = {}
    job_role_coverage = Counter()
    oldest = None
    newest = None
    for d in decisions:
        actions[str(d.candidate_action or "UNKNOWN")] += 1
        decision_risk_status[str(d.risk_status or "UNKNOWN")] += 1
        risks_for = risk_map.get(d.id, [])
        orders_for = order_map.get(d.id, [])
        related = [
            a for a in agent_map.get(d.correlation_id, [])
            if a.created_at is not None and a.created_at <= d.created_at
        ]
        # The presence of a risk check is not mandatory for WAIT/HOLD/BLOCK.
        risk_coverage["has_risk_check" if risks_for else "no_visible_risk_check"] += 1
        order_coverage["has_paper_order" if orders_for else "no_visible_paper_order"] += 1
        agent_coverage["has_predecision_agent_output" if related
                       else "no_visible_predecision_agent_output"] += 1
        jobs_for = job_map.get(d.correlation_id, [])
        predecision_complete = [
            job for job in jobs_for
            if job.status == "COMPLETED"
            and job.completed_at is not None
            and job.completed_at <= d.created_at
            and job.output_payload is not None
        ]
        status = (
            "has_completed_predecision_job" if predecision_complete
            else "job_visible_but_not_completed_before_decision" if jobs_for
            else "no_visible_execution_job"
        )
        job_coverage[status] += 1
        for role in {str(job.decision_role) for job in predecision_complete}:
            job_role_coverage[role] += 1
        action = str(d.candidate_action or "UNKNOWN")
        group = action_funnel.setdefault(action, Counter())
        group["decisions"] += 1
        group["has_risk_check" if risks_for else "no_visible_risk_check"] += 1
        group["has_paper_order" if orders_for else "no_visible_paper_order"] += 1
        group["legacy_output_visible" if related else "no_legacy_output"] += 1
        group["completed_job_visible" if predecision_complete
              else "no_completed_job"] += 1
        group["predecision_evidence_in_either_source" if
              (related or predecision_complete) else
              "no_predecision_evidence_in_either_source"] += 1
        group["risk_status_" + str(d.risk_status or "UNKNOWN")] += 1
        if d.created_at is not None:
            moment = d.created_at
            if moment.tzinfo is None:
                raise ValueError("Decision processing clock lacks timezone")
            utc = moment.astimezone(timezone.utc)
            oldest = min(oldest, utc) if oldest is not None else utc
            newest = max(newest, utc) if newest is not None else utc
    return {
        "mode": "HISTORICAL_BOUNDED_READ_ONLY_PREFLIGHT",
        "prospective_collection_running": False,
        "candidate_visibility_complete": False,
        "sampled_decisions": len(decisions),
        "sample_limit": sampled_limit,
        "has_more_decisions_than_limit": bool(has_more_decisions),
        "oldest_sampled_decision_utc": oldest.isoformat() if oldest else None,
        "newest_sampled_decision_utc": newest.isoformat() if newest else None,
        "candidate_actions": dict(sorted(actions.items())),
        "decision_risk_statuses": dict(sorted(decision_risk_status.items())),
        "risk_check_visibility": dict(sorted(risk_coverage.items())),
        "paper_order_visibility": dict(sorted(order_coverage.items())),
        "predecision_agent_visibility": dict(sorted(agent_coverage.items())),
        "execution_job_visibility": dict(sorted(job_coverage.items())),
        "completed_job_role_visibility": dict(sorted(job_role_coverage.items())),
        "action_funnel": {
            key: dict(sorted(group.items()))
            for key, group in sorted(action_funnel.items())
        },
        "next_step": "VERSIONED_PROSPECTIVE_CAPTURE_OF_ALL_CANDIDATES_REQUIRED",
        "caveats": [
            "Sample is bounded and may omit older, pruned or RLS-hidden decisions.",
            "A decision row is not proof of full opportunity or candle coverage.",
            "WAIT/HOLD/BLOCK normally require no order and may require no risk check.",
            "An agent output sharing correlation ID is observational, not causal.",
            "Modern agent_execution_jobs are a separate source from legacy agent_outputs.",
            "Jobs and legacy outputs are not guaranteed to represent all agent opinions.",
            "A completed pre-decision job does not prove its signal was used in consensus.",
            "This report uses historical records and cannot validate future OOS profitability.",
            "No scheduling or automatic worker was started.",
        ],
    }

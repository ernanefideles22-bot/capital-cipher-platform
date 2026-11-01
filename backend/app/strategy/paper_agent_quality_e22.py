"""E2.2 PAPER research: versioned forecast quality and job coverage only.

Read-only snapshot aggregation; never selects weights or places orders.
Forecast accuracy is a ONE-BAR predictive metric, not realized net trading
profit. No independent prospective holdout or fill observability established.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timezone
from math import isfinite
from statistics import fmean

MIN_SETTLED = 30
MAX_CARDS = 30


def _aware(value):
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware provenance mandatory")
    return value.astimezone(timezone.utc)


def _number(value):
    x = float(value)
    if not isfinite(x):
        raise ValueError("Finite numeric forecast evidence required")
    return x


def evaluate_runtime_quality(
    forecasts, outcomes, jobs, *, forecasts_truncated: bool,
    jobs_truncated: bool, forecast_table_visible: bool,
    outcome_table_visible: bool, jobs_table_visible: bool,
) -> dict:
    """Aggregate independently persisted outcomes; never infer missing rows.

    Observation is authoritative only when target_at == realized_at and
    creation time <= forecast target. The known internal probability model
    maps BUY/SELL confidence into probability_up, not executable returns.
    """
    result = {
        "mode": "READ_ONLY_HISTORICAL_AGENT_FORECAST_PREFLIGHT",
        "prospective_forward_collection_confirmed": False,
        "complete_300_agent_coverage_confirmed": False,
        "agent_profitability_proven": False,
        "quality_model": "ONE_CANDLE_DIRECTIONAL_BRIER_NOT_NET_TRADING_RETURNS",
        "minimum_settled_per_agent_version_for_descriptive_metrics": MIN_SETTLED,
        "tables_visible": {
            "agent_forecasts": bool(forecast_table_visible),
            "agent_forecast_outcomes": bool(outcome_table_visible),
            "agent_execution_jobs": bool(jobs_table_visible),
        },
        "forecast_sample_truncated": bool(forecasts_truncated),
        "job_sample_truncated": bool(jobs_truncated),
    }
    if not jobs_table_visible:
        if jobs:
            raise ValueError("Jobs supplied although table is not readable")
        result["job_coverage"] = {"status": "TABLE_NOT_VISIBLE_OR_ABSENT"}
    else:
        job_ids = set()
        agent_versions = set()
        statuses = Counter()
        roles = Counter()
        samples_per_agent = Counter()
        for j in jobs:
            if j.execution_id in job_ids:
                raise ValueError("Duplicate runtime execution")
            job_ids.add(j.execution_id)
            key = (str(j.agent_name), str(j.agent_version))
            agent_versions.add(key)
            samples_per_agent[key] += 1
            statuses[str(j.status)] += 1
            roles[str(j.decision_role)] += 1
        result["job_coverage"] = {
            "status": "BOUNDED_VISIBLE_SAMPLE_NOT_TOTAL_REGISTRY",
            "sampled_execution_jobs": len(jobs),
            "distinct_agent_names": len({name for name, _ in agent_versions}),
            "distinct_agent_versions": len(agent_versions),
            "jobs_by_status": dict(sorted(statuses.items())),
            "jobs_by_role": dict(sorted(roles.items())),
            "distinct_agent_versions_with_multiple_jobs":
                sum(n > 1 for n in samples_per_agent.values()),
            "complete_agent_registry": False,
        }
    if not forecast_table_visible or not outcome_table_visible:
        if forecasts or outcomes:
            raise ValueError("Forecast/outcome data without visible source")
        result["forecasts"] = {
            "status": "FORECAST_OR_OUTCOME_TABLE_NOT_VISIBLE",
            "settled_count": None,
            "unsettled_count": None,
            "agent_version_quality_cards": [],
        }
        return result

    indexed = {}
    for f in forecasts:
        if f.forecast_id in indexed:
            raise ValueError("Duplicate forecast row")
        indexed[f.forecast_id] = f
    o_by_forecast = {}
    for o in outcomes:
        if o.forecast_id in o_by_forecast:
            raise ValueError("Duplicate realized outcome")
        if o.forecast_id not in indexed:
            raise ValueError("Outcome without visible forecast")
        o_by_forecast[o.forecast_id] = o

    status = Counter()
    eligible = defaultdict(list)
    signals = Counter()
    sampled_roles = Counter()
    invalid_time = 0
    invalid_data = 0
    directional_forecasts = 0
    for f in forecasts:
        role = str(f.decision_role)
        sampled_roles[role] += 1
        signal = str(f.signal)
        signals[signal] += 1
        if signal in ("BUY", "SELL"):
            directional_forecasts += 1
        if signal not in {"BUY", "SELL", "HOLD", "WAIT", "BLOCK", "NEUTRAL"}:
            invalid_data += 1
            continue
        try:
            predicted = _number(f.probability_up)
            forecast_at = _aware(f.forecast_at)
            created_at = _aware(f.created_at)
            target = _aware(f.target_at)
            if not (0 <= predicted <= 1 and forecast_at <= target
                    and created_at < target):
                invalid_time += 1
                continue
        except (ValueError, TypeError, OverflowError):
            invalid_data += 1
            continue
        o = o_by_forecast.get(f.forecast_id)
        if o is None:
            status["unsettled_visible"] += 1
            continue
        try:
            realized_at = _aware(o.realized_at)
            actual = _number(o.realized_up)
            if realized_at != target or not 0 <= actual <= 1:
                invalid_time += 1
                continue
        except (ValueError, TypeError, OverflowError):
            invalid_data += 1
            continue
        # Group by immutable agent/version + venue universe + role. Never
        # combine a SHADOW forecaster into a PRIMARY version's accuracy.
        key = (str(f.agent_name), str(f.agent_version), role,
               str(f.symbol), str(f.timeframe))
        brier = (predicted - actual) ** 2
        direction_correct = (
            (predicted > .5) == (actual > .5)
            if predicted != .5 and actual != .5 else None
        )
        eligible[key].append((brier, direction_correct, predicted, actual))
        status["settled_verified"] += 1

    cards = []
    for (agent, version, role, symbol, timeframe), samples in eligible.items():
        n = len(samples)
        directional = [x[1] for x in samples if x[1] is not None]
        brier = fmean(x[0] for x in samples)
        # Predicting 0.5 is a simple fixed null benchmark, not a calibrated
        # universe-specific base rate; ties remain in the Brier score.
        neutral_brier = fmean((.5 - x[3]) ** 2 for x in samples)
        cards.append({
            "agent_name": agent, "agent_version": version,
            "decision_role": role, "symbol": symbol, "timeframe": timeframe,
            "settled_forecasts": n,
            "directional_forecasts_settled": len(directional),
            "directional_accuracy": round(sum(directional) / len(directional), 6)
                                     if directional else None,
            "mean_brier": round(brier, 6),
            "constant_half_brier": round(neutral_brier, 6),
            "brier_improvement_vs_constant_half": round(neutral_brier - brier, 6),
            "sufficient_for_descriptive_card": n >= MIN_SETTLED,
            "strategy_or_weight_promotion_authorized": False,
        })
    # Select by coverage, not in-sample Brier or P&L, to avoid a false leaderboard.
    cards.sort(key=lambda x: (-x["settled_forecasts"], x["agent_name"],
                              x["agent_version"], x["symbol"], x["timeframe"]))
    result["forecasts"] = {
        "status": "HISTORICAL_BOUNDED_NOT_PROSPECTIVE",
        "sampled_forecasts": len(forecasts),
        "outcomes_joined": len(outcomes),
        "directional_forecasts_sampled": directional_forecasts,
        "settled_count": status["settled_verified"],
        "unsettled_count": status["unsettled_visible"],
        "invalid_timing_count": invalid_time,
        "invalid_content_count": invalid_data,
        "sampled_signals": dict(sorted(signals.items())),
        "sampled_roles": dict(sorted(sampled_roles.items())),
        "distinct_settled_agent_version_universes": len(cards),
        "descriptively_eligible_cards": sum(
            c["sufficient_for_descriptive_card"] for c in cards
        ),
        "agent_version_quality_cards": cards[:MAX_CARDS],
        "quality_cards_truncated": len(cards) > MAX_CARDS,
        "scorecards_ordering": "COVERAGE_ONLY_NOT_PERFORMANCE_RANK",
    }
    result["limitations"] = [
        "Forecast and execution job samples are bounded and subject to retention/RLS.",
        "One-bar forecasts are correlated, share market bars and may not be independent observations.",
        "A Brier score is a probability calibration metric, not expected net PnL.",
        "No per-agent executed position, fee, slippage or economic attribution verified.",
        "A fixed p=0.5 benchmark is not a market prevalence-corrected baseline.",
        "Historical evidence was already accessible: no sealed, prospective OOS test.",
        "Candidate/agent parameter changes must NOT be selected on these same outcomes.",
        "No automated worker was started; future data accumulation is NOT confirmed.",
        "No change to orders, agents, risk limits, OMS or LIVE/TESTNET is authorized.",
    ]
    return result

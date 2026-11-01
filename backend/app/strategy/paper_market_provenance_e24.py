"""E2.4: cross-agent candle independence and bounded stored-observation evidence.

No write or operational authority. An observation match does NOT authenticate
external exchange origin (synthetic/backfilled candles can also be persisted).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite
from sqlalchemy import and_, func, select

from app.database.models import AgentForecastModel as F
from app.database.models import AgentForecastOutcomeModel as O

WINDOW_DAYS = 7
SERIES_LIMIT = 40
EVENT_PROBE_LIMIT = 90
OBSERVATION_LIMIT = 600
PRICE_REL_TOL = 1e-6


def _cutoff(now: datetime):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UTC-aware clock mandatory")
    now = now.astimezone(timezone.utc)
    return now - timedelta(days=WINDOW_DAYS), now


def _base_query(now: datetime):
    cutoff, now = _cutoff(now)
    return (
        select(F)
        .join(O, F.forecast_id == O.forecast_id)
        .where(
            F.created_at >= cutoff, F.created_at < F.target_at,
            F.forecast_at <= F.created_at,
            O.realized_at == F.target_at, F.target_at <= now,
        )
    )


def independence_query(now: datetime):
    """All settled forecasts grouped by series; count distinct target events."""
    cutoff, now = _cutoff(now)
    return (
        select(
            F.symbol, F.timeframe,
            func.count(F.forecast_id).label("settled"),
            func.count(func.distinct(F.target_at)).label("distinct_target_events"),
            func.count(func.distinct(F.correlation_id)).label("distinct_correlations"),
            func.count(func.distinct(F.agent_name)).label("distinct_agent_names"),
            func.min(F.target_at).label("oldest_target_at"),
            func.max(F.target_at).label("newest_target_at"),
        )
        .join(O, F.forecast_id == O.forecast_id)
        .where(
            F.created_at >= cutoff, F.created_at < F.target_at,
            F.forecast_at <= F.created_at,
            O.realized_at == F.target_at, F.target_at <= now,
        )
        .group_by(F.symbol, F.timeframe)
        .order_by(func.count(F.forecast_id).desc(), F.symbol, F.timeframe)
        .limit(SERIES_LIMIT + 1)
    )


def event_probe_query(now: datetime):
    """One row per forecast->target market event, not one row per agent."""
    cutoff, now = _cutoff(now)
    return (
        select(
            F.symbol, F.timeframe, F.forecast_at, F.target_at,
            func.count(F.forecast_id).label("forecast_count"),
            func.min(F.reference_price).label("reference_min"),
            func.max(F.reference_price).label("reference_max"),
            func.min(O.realized_price).label("outcome_min"),
            func.max(O.realized_price).label("outcome_max"),
            func.min(F.created_at).label("first_forecast_created"),
            func.min(O.created_at).label("first_outcome_created"),
        )
        .join(O, F.forecast_id == O.forecast_id)
        .where(
            F.created_at >= cutoff, F.created_at < F.target_at,
            F.forecast_at <= F.created_at,
            O.realized_at == F.target_at, F.target_at <= now,
        )
        .group_by(F.symbol, F.timeframe, F.forecast_at, F.target_at)
        .order_by(F.target_at.desc(), F.symbol, F.timeframe)
        .limit(EVENT_PROBE_LIMIT + 1)
    )


def _same_price(a, b):
    a, b = float(a), float(b)
    return isfinite(a) and isfinite(b) and a > 0 and b > 0 and (
        abs(a-b) <= max(abs(a),abs(b))*PRICE_REL_TOL
    )


def summarize_market_evidence(
    series_rows, sampled_events, candle_observations, *,
    observation_table_visible: bool, now: datetime,
) -> dict:
    _cutoff(now)
    if len(series_rows) > SERIES_LIMIT + 1 or len(sampled_events) > EVENT_PROBE_LIMIT + 1:
        raise ValueError("Unbounded market cohort")
    report = {
        "mode": "E24_HISTORICAL_MARKET_INDEPENDENCE_AND_CANDLE_PROBE",
        "window_days": WINDOW_DAYS,
        "series_limit": SERIES_LIMIT,
        "event_probe_limit": EVENT_PROBE_LIMIT,
        "prospective_evidence_confirmed": False,
        "external_exchange_source_authenticated": False,
        "profitability_or_agent_promotion_authorized": False,
        "source_independence": "DISTINCT_TARGET_PER_SYMBOL_TIMEFRAME_NOT_INDEPENDENT_PNL",
        "observation_table_visible": bool(observation_table_visible),
    }
    series = []
    for r in series_rows[:SERIES_LIMIT]:
        n, targets, corr, agents = (
            int(r.settled), int(r.distinct_target_events),
            int(r.distinct_correlations), int(r.distinct_agent_names)
        )
        if not (n >= targets > 0 and n >= corr > 0 and n >= agents > 0):
            raise ValueError("Invalid market independence cardinality")
        series.append({
            "symbol": str(r.symbol), "timeframe": str(r.timeframe),
            "settled_forecasts": n,
            "distinct_target_events": targets,
            "distinct_correlations": corr,
            "distinct_agent_names": agents,
            "forecasts_per_target_event": round(n / targets, 3),
            "oldest_target_at": r.oldest_target_at.isoformat(),
            "newest_target_at": r.newest_target_at.isoformat(),
        })
    summary = {
        "series": series,
        "series_truncated": len(series_rows) > SERIES_LIMIT,
        "totals_complete": len(series_rows) <= SERIES_LIMIT,
        "total_settled_forecasts": sum(x["settled_forecasts"] for x in series),
        "distinct_series_target_events": sum(x["distinct_target_events"] for x in series),
        "distinct_events_are_independent_across_assets": False,
    }
    if summary["distinct_series_target_events"]:
        summary["forecasts_per_distinct_series_target_event"] = round(
            summary["total_settled_forecasts"] /
            summary["distinct_series_target_events"], 3,
        )
    report["independence"] = summary

    if not observation_table_visible:
        if candle_observations:
            raise ValueError("Invisible observation source returned rows")
        report["observation_probe"] = {
            "status": "TABLE_NOT_VISIBLE_OR_NOT_AUTHORIZED",
            "events_in_sample": len(sampled_events[:EVENT_PROBE_LIMIT]),
            "not_a_source_proof": True,
        }
        return report
    if len(candle_observations) > OBSERVATION_LIMIT:
        raise ValueError("Unbounded observations")

    obs_by_key = defaultdict(list)
    for c in candle_observations:
        obs_by_key[(str(c.symbol), str(c.timeframe), c.closed_at)].append(c)
    counters = Counter()
    exchange_counts = Counter()
    quality_counts = Counter()
    for evt in sampled_events[:EVENT_PROBE_LIMIT]:
        counters["probed_distinct_forecast_target_events"] += 1
        sym, tf = str(evt.symbol), str(evt.timeframe)
        ref = obs_by_key.get((sym, tf, evt.forecast_at), ())
        target = obs_by_key.get((sym, tf, evt.target_at), ())
        if not _same_price(evt.reference_min, evt.reference_max):
            counters["cohort_disagrees_on_reference_price"] += 1
        if not _same_price(evt.outcome_min, evt.outcome_max):
            counters["cohort_disagrees_on_realized_price"] += 1
        # Root-cause matrix is deliberately aggregate-only. A price match
        # alone does not establish either timely receipt or timely persistence.
        ref_price_matches = [
            x for x in ref
            if _same_price(x.close, evt.reference_min)
            and _same_price(x.close, evt.reference_max)
        ]
        target_price_matches = [
            x for x in target
            if _same_price(x.close, evt.outcome_min)
            and _same_price(x.close, evt.outcome_max)
        ]
        for phase, rows, prices, created in (
            ("reference", ref, ref_price_matches, evt.first_forecast_created),
            ("target", target, target_price_matches, evt.first_outcome_created),
        ):
            if not rows:
                counters[phase + "_observation_absent"] += 1
            elif not prices:
                counters[phase + "_price_mismatch"] += 1
            else:
                counters[phase + "_price_match_ignore_timing"] += 1
                received = [
                    x for x in prices if x.received_at <= created
                    and x.received_at >= x.closed_at
                ]
                recorded = [
                    x for x in prices if x.recorded_at <= created
                ]
                if received:
                    counters[phase + "_receipt_before_creation"] += 1
                else:
                    counters[phase + "_receipt_too_late_or_clock_invalid"] += 1
                if recorded:
                    counters[phase + "_stored_before_creation"] += 1
                else:
                    counters[phase + "_stored_after_creation"] += 1
                if any(
                    x.received_at <= created
                    and x.recorded_at <= created
                    and x.received_at >= x.closed_at
                    for x in prices
                ):
                    counters[phase + "_asof_price_and_time_match"] += 1
                elif received and recorded:
                    counters[phase + "_receipt_store_on_different_rows"] += 1
        before_ref = [
            x for x in ref_price_matches
            if x.received_at <= evt.first_forecast_created
            and x.recorded_at <= evt.first_forecast_created
            and x.received_at >= x.closed_at
        ]
        before_target = [
            x for x in target_price_matches
            if x.received_at <= evt.first_outcome_created
            and x.recorded_at <= evt.first_outcome_created
            and x.received_at >= x.closed_at
        ]
        if ref:
            counters["reference_observation_time_exists"] += 1
        if target:
            counters["target_observation_time_exists"] += 1
        if before_ref:
            counters["reference_price_and_asof_recorded_match"] += 1
        if before_target:
            counters["realized_price_and_asof_recorded_match"] += 1
        paired_exchanges = {str(x.exchange) for x in before_ref} & {
            str(x.exchange) for x in before_target
        }
        if paired_exchanges:
            counters["reference_and_target_matching_same_exchange"] += 1
            if len(paired_exchanges) != 1:
                counters["multiple_exchanges_match_same_event"] += 1
            for exchange in paired_exchanges:
                exchange_counts[exchange] += 1
            for x in before_ref:
                if str(x.exchange) in paired_exchanges:
                    quality_counts[str(x.quality_status)] += 1
        if (not before_ref or not before_target) and (ref or target):
            counters["price_or_asof_provenance_gap"] += 1

    report["observation_probe"] = {
        "status": "BOUNDED_CANDLE_TABLE_CROSSCHECK_NOT_EXTERNAL_AUTHENTICATION",
        "events_in_sample": counters["probed_distinct_forecast_target_events"],
        "event_probe_truncated": len(sampled_events) > EVENT_PROBE_LIMIT,
        "observation_rows_used": len(candle_observations),
        "observation_rows_truncated": len(candle_observations) == OBSERVATION_LIMIT,
        "counts": dict(sorted(counters.items())),
        "matching_exchange_labels": dict(sorted(exchange_counts.items())),
        "matching_reference_quality_labels": dict(sorted(quality_counts.items())),
        "not_a_source_proof": True,
    }
    report["limitations"] = [
        "Same candle evaluated by 300 agents is ONE target event, not 300 independent market observations.",
        "Different assets/timeframes are market-correlated; sums overstate effective independence.",
        "A matching stored candle does not prove genuine exchange origin; synthetic candles may be persisted.",
        "Forecast rows lack explicit exchange and immutable source candle ID; a same-exchange match can be ambiguous.",
        "A missing observation may be RLS, retention, different table, or a real provenance problem.",
        "Probe checks only a bounded recent sample and cannot certify every event or audit raw payload signatures.",
        "A candle observed after forecast creation can be a legitimate late persistence path, a clock discrepancy or a synthetic/backfilled source; the audit does not infer which.",
        "No future holdout, execution costs, strategy optimization or agent authority enabled.",
    ]
    return report

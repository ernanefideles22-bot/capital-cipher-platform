"""E2.3 read-only SQL aggregation across a longer window than the E2.2 row cap.

Never materializes all forecasts and never ranks strategies for promotion.
Uses the existing indexed forecast/outcome tables without migrations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

from sqlalchemy import and_, case, func, or_, select, literal

from app.database.models import AgentForecastModel as F
from app.database.models import AgentForecastOutcomeModel as O

WINDOW_DAYS = 7
GROUP_LIMIT = 900
DISPLAY_LIMIT = 25
MIN_DESCRIPTIVE = 30


def cohort_query(now: datetime):
    """One grouped SELECT over a fixed trailing seven-day forecast window.

    A forecast must be created before its target and its outcome must match
    target time. Joining on a UNIQUE forecast_id prevents double counting.
    Aggregated metrics are for historical calibration, not trade P&L.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UTC-aware now required")
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)
    correct = or_(
        and_(F.probability_up > .5, O.realized_up > .5),
        and_(F.probability_up < .5, O.realized_up < .5),
    )
    directional = and_(
        F.probability_up != .5, O.realized_up != .5,
        F.signal.in_(("BUY", "SELL")),
    )
    brier = (F.probability_up - O.realized_up) * (
        F.probability_up - O.realized_up
    )
    null_brier = (literal(.5) - O.realized_up) * (
        literal(.5) - O.realized_up
    )
    dimensions = (
        F.agent_name, F.agent_version, F.decision_role, F.symbol, F.timeframe
    )
    return (
        select(
            *dimensions,
            func.count(O.forecast_id).label("settled"),
            func.sum(case((directional, 1), else_=0)).label("directional"),
            func.sum(case((and_(directional, correct), 1), else_=0)).label("correct"),
            func.avg(brier).label("mean_brier"),
            func.avg(null_brier).label("mean_neutral_brier"),
        )
        .select_from(F)
        .join(O, F.forecast_id == O.forecast_id)
        .where(
            F.created_at >= cutoff,
            F.created_at < F.target_at,
            F.forecast_at <= F.target_at,
            F.target_at <= now,
            O.realized_at == F.target_at,
        )
        .group_by(*dimensions)
        .order_by(func.count(O.forecast_id).desc(), *dimensions)
        .limit(GROUP_LIMIT + 1)
    )


def summarize_cohort_rows(rows, *, tables_visible: bool, now: datetime) -> dict:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("UTC-aware now required")
    report = {
        "mode": "HISTORICAL_SEVEN_DAY_SQL_AGGREGATE_NO_EXECUTION",
        "window_days": WINDOW_DAYS,
        "closed_at_utc": now.astimezone(timezone.utc).isoformat(),
        "group_limit": GROUP_LIMIT,
        "min_settled_for_descriptive_metrics": MIN_DESCRIPTIVE,
        "trading_authorized": False,
        "agent_skill_or_profitability_proven": False,
        "prospective_study_started": False,
        "source_visible": bool(tables_visible),
        "caveats": [
            "Retrospective seven-day window, not independent prospective OOS data.",
            "Multiple forecasts on the same candle are correlated observations.",
            "Brier/directional quality does not account for trade fills, fees or stop losses.",
            "Group cap bounds reporting, not database scan; retained history/RLS may still hide records.",
            "No agent weights, permissions or risk rules are changed.",
        ],
    }
    if not tables_visible:
        if rows:
            raise ValueError("Rows returned from invisible source")
        report["status"] = "SOURCE_NOT_VISIBLE"
        report["groups_scanned"] = None
        report["descriptive_groups"] = None
        report["cards"] = []
        return report

    if len(rows) > GROUP_LIMIT + 1:
        raise ValueError("Unbounded grouped result")
    truncated = len(rows) > GROUP_LIMIT
    seen = set()
    cards = []
    for row in rows[:GROUP_LIMIT]:
        # SQLAlchemy Row and mapped test rows both use attribute access.
        fields = (
            str(row.agent_name), str(row.agent_version),
            str(row.decision_role), str(row.symbol), str(row.timeframe)
        )
        if fields in seen:
            raise ValueError("Duplicate group in aggregation")
        seen.add(fields)
        n, d, c = int(row.settled), int(row.directional), int(row.correct)
        brier, null_brier = float(row.mean_brier), float(row.mean_neutral_brier)
        if (n <= 0 or not (0 <= c <= d <= n) or not all(isfinite(x)
                and 0 <= x <= 1 for x in (brier, null_brier))):
            raise ValueError("Invalid aggregate counts or Brier")
        cards.append({
            "agent_name": fields[0],
            "agent_version": fields[1],
            "decision_role": fields[2],
            "symbol": fields[3],
            "timeframe": fields[4],
            "settled": n,
            "directional": d,
            "directional_accuracy": round(c / d, 6) if d else None,
            "mean_brier": round(brier, 6),
            "neutral_brier": round(null_brier, 6),
            "brier_advantage": round(null_brier - brier, 6),
            "sufficient_for_descriptive_metrics": n >= MIN_DESCRIPTIVE,
            "eligible_for_promotion": False,
        })
    cards.sort(key=lambda x: (
        -x["settled"], x["agent_name"], x["agent_version"],
        x["symbol"], x["timeframe"],
    ))
    report.update({
        "status": "BOUNDED_AGGREGATE",
        "groups_scanned": len(cards),
        "groups_truncated": truncated,
        "descriptive_groups": sum(
            x["sufficient_for_descriptive_metrics"] for x in cards
        ),
        "groups_without_30_settled": sum(
            not x["sufficient_for_descriptive_metrics"] for x in cards
        ),
        "settled_forecasts_in_scanned_groups": sum(x["settled"] for x in cards),
        "cards": cards[:DISPLAY_LIMIT],
        "cards_truncated": len(cards) > DISPLAY_LIMIT,
        "sort_order": "SETTLED_COVERAGE_NOT_BEST_BRIER_OR_PROFIT",
        "group_counts_are_complete": not truncated,
    })
    return report

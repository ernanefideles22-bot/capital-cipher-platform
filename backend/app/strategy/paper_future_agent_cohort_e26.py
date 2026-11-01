"""E2.6 future-window per-agent versioned SHADOW forecasting cohort (read-only).

This reads ALREADY PRODUCED runtime forecasts; it does not schedule 300 agents,
create new predictions or submit orders. Join to actual runtime job identity
and submission clock instead of treating forecast.created_at (candle receipt)
as actual database insertion time. In-sample correlation remains substantial.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

from sqlalchemy import and_, or_, case, exists, func, literal, select

from app.database.models import (
    AgentExecutionJobModel as J,
    AgentForecastModel as F,
    AgentForecastOutcomeModel as O,
)
from app.strategy.prospective_core_gate_e26 import CAMPAIGN

GROUP_LIMIT = 900
CARD_LIMIT = 25
MIN_DESCRIPTIVE = 30
START = datetime.fromisoformat(
    CAMPAIGN["first_evaluation_candle_closed_at_utc"]
)
END = datetime.fromisoformat(
    CAMPAIGN["evaluation_candle_end_exclusive_utc"]
)


def readiness(now: datetime, *, sources_visible: bool) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware actual clock required")
    current = now.astimezone(timezone.utc)
    if not sources_visible:
        return "SOURCE_TABLES_NOT_VISIBLE"
    if current < START:
        return "FUTURE_WINDOW_NOT_STARTED"
    if current < END:
        return "FUTURE_WINDOW_OPEN_INTERIM_ONLY"
    return "FUTURE_WINDOW_CLOSED_UNATTESTED"


def per_agent_query(now: datetime):
    """Immutable SQL group, scoped to future target bars and observed jobs."""
    if readiness(now, sources_visible=True) == "FUTURE_WINDOW_NOT_STARTED":
        raise ValueError("Cannot query agent scores before future window starts")
    current = now.astimezone(timezone.utc)
    asof = min(current, END)
    directional = and_(
        F.probability_up != 0.5,
        O.realized_up != 0.5,
        F.signal.in_(("BUY", "SELL")),
    )
    correct = and_(
        directional,
        or_(
            and_(F.probability_up > .5, O.realized_up > .5),
            and_(F.probability_up < .5, O.realized_up < .5),
        ),
    )
    brier = (F.probability_up-O.realized_up)*(
        F.probability_up-O.realized_up
    )
    neutral = (literal(0.5)-O.realized_up)*(
        literal(0.5)-O.realized_up
    )
    # EXISTS avoids multiplying a forecast when an agent retries a runtime job.
    # J.created_at is actual submission time, unlike F.created_at.
    actual_forward_job = exists(
        select(J.execution_id).where(
            J.correlation_id == F.correlation_id,
            J.agent_name == F.agent_name,
            J.agent_version == F.agent_version,
            J.decision_role == F.decision_role,
            J.execution_mode == "PAPER",
            J.created_at >= START-timedelta(hours=4),
            J.created_at < F.target_at,
            J.created_at <= current,
        )
    )
    dims=(
        F.agent_name,F.agent_version,F.decision_role,F.symbol,F.timeframe,
    )
    return (
        select(
            *dims,
            func.count(F.forecast_id).label("settled"),
            func.count(func.distinct(F.target_at)).label("distinct_target_events"),
            func.sum(case((directional,1),else_=0)).label("directional"),
            func.sum(case((correct,1),else_=0)).label("correct"),
            func.avg(brier).label("mean_brier"),
            func.avg(neutral).label("mean_neutral_brier"),
        )
        .join(O,F.forecast_id == O.forecast_id)
        .where(
            F.target_at >= START,
            F.target_at < END,
            F.target_at <= asof,
            F.forecast_at < F.target_at,
            F.created_at < F.target_at,
            F.forecast_at <= F.created_at,
            O.realized_at == F.target_at,
            F.probability_up >= 0,F.probability_up <= 1,
            O.realized_up >= 0,O.realized_up <= 1,
            actual_forward_job,
        )
        .group_by(*dims)
        .order_by(func.count(F.forecast_id).desc(),*dims)
        .limit(GROUP_LIMIT+1)
    )


def summarize(rows, *, now: datetime, sources_visible: bool) -> dict:
    status=readiness(now,sources_visible=sources_visible)
    if len(rows)>GROUP_LIMIT+1:
        raise ValueError("Unbounded forward agent cohort")
    if status in ("FUTURE_WINDOW_NOT_STARTED","SOURCE_TABLES_NOT_VISIBLE") and rows:
        raise ValueError("Evidence cannot exist for unavailable forward window")
    cards=[]
    for row in rows[:GROUP_LIMIT]:
        settled=int(row.settled)
        distinct=int(row.distinct_target_events)
        direct=int(row.directional)
        right=int(row.correct)
        b=float(row.mean_brier)
        neutral=float(row.mean_neutral_brier)
        if not (0<=right<=direct<=settled and 0<=distinct<=settled and
                all(isfinite(x) and 0<=x<=1 for x in (b,neutral))):
            raise ValueError("Invalid forward agent evidence")
        cards.append({
            "agent_name":str(row.agent_name),
            "agent_version":str(row.agent_version),
            "decision_role":str(row.decision_role),
            "symbol":str(row.symbol),
            "timeframe":str(row.timeframe),
            "settled_forecasts":settled,
            "distinct_target_events":distinct,
            "directional_accuracy":round(right/direct,6) if direct else None,
            "mean_brier":round(b,6),
            "brier_improvement_vs_constant_half":round(neutral-b,6),
            "descriptively_eligible":(
                settled>=MIN_DESCRIPTIVE and
                distinct>=MIN_DESCRIPTIVE
            ),
            "strategy_promotion_permitted":False,
        })
    return {
        "mode":"E26_FUTURE_VERSIONED_AGENT_SHADOW_ONLY",
        "status":status,
        "first_target_at":START.isoformat(),
        "end_exclusive_at":END.isoformat(),
        "group_limit":GROUP_LIMIT,
        "group_sample_truncated":len(rows)>GROUP_LIMIT,
        "groups_visible":len(cards),
        "descriptive_groups":sum(x["descriptively_eligible"] for x in cards),
        "cards":cards[:CARD_LIMIT],
        "cards_truncated":len(cards)>CARD_LIMIT,
        "coverage_order_not_performance_rank":True,
        "effective_independent_events_not_proven":True,
        "source_authenticated_by_independent_exchange":False,
        "prospective_forecast_ledger_independently_sealed":False,
        "profitability_proven":False,
        "automatic_promotion_authorized":False,
        "execution_authorized":False,
        "limitations":[
            "Existing 300-agent runtime jobs are read if available but no new agents are run by this component.",
            "Matching PAPER job timestamps prevent naive backdating but are not an external audit signature.",
            "Forecasts can be strongly dependent when many agents predict the same asset/candle.",
            "Brier/directional skill is not net P&L, OMS fill or trade expectancy.",
            "Partial-window cards must not be used for agent selection or strategy changes.",
            "No strategy weight, OMS, risk, LIVE/TESTNET or account change permitted.",
        ],
    }

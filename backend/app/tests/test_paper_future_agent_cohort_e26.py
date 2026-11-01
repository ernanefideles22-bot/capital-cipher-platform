"""E2.6 per-agent future SHADOW scores are coverage-only and non-executable."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from app.strategy.paper_future_agent_cohort_e26 import (
    START, END, GROUP_LIMIT, per_agent_query, readiness, summarize,
)


def row(*, n=40, events=40, directional=35, correct=20,
        score=.23, neutral=.25):
    return SimpleNamespace(
        agent_name="TraderCore01",agent_version="v1",decision_role="SHADOW",
        symbol="BTCUSDT",timeframe="15m",
        settled=n,distinct_target_events=events,
        directional=directional,correct=correct,
        mean_brier=score,mean_neutral_brier=neutral,
    )


def test_future_status_refuses_prestudy_and_missing_source():
    now=datetime(2026,11,1,tzinfo=timezone.utc)
    assert readiness(now,sources_visible=True)=="FUTURE_WINDOW_NOT_STARTED"
    z=summarize([],now=now,sources_visible=True)
    assert z["groups_visible"]==0
    assert not z["execution_authorized"]
    assert not z["profitability_proven"]
    assert readiness(now,sources_visible=False)=="SOURCE_TABLES_NOT_VISIBLE"
    with pytest.raises(ValueError,match="before future"):
        per_agent_query(now)
    with pytest.raises(ValueError,match="cannot exist"):
        summarize([row()],now=now,sources_visible=True)


def test_query_ties_real_job_identity_to_future_target_without_backfill():
    at=START+__import__("datetime").timedelta(hours=4)
    statement=per_agent_query(at)
    assert isinstance(statement, Select)
    sql=str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds":True},
    )).lower()
    assert "exists" in sql
    assert "agent_execution_jobs" in sql
    assert "agent_forecasts" in sql and "agent_forecast_outcomes" in sql
    assert "2026-11-05" in sql
    assert "execution_mode" in sql
    assert "correlation_id" in sql
    assert "target_at" in sql and "created_at" in sql
    assert "group by" in sql
    assert "limit 901" in sql


def test_30_distinct_events_only_descriptive_not_profit_validation():
    now=START+__import__("datetime").timedelta(days=5)
    z=summarize([row()],now=now,sources_visible=True)
    assert z["status"]=="FUTURE_WINDOW_OPEN_INTERIM_ONLY"
    assert z["descriptive_groups"]==1
    assert z["cards"][0]["directional_accuracy"]==round(20/35,6)
    assert z["cards"][0]["descriptively_eligible"]
    assert not z["cards"][0]["strategy_promotion_permitted"]
    assert z["source_authenticated_by_independent_exchange"] is False
    assert z["prospective_forecast_ledger_independently_sealed"] is False


def test_300_forecasts_on_one_market_event_does_not_count_as_30_events():
    now=END+__import__("datetime").timedelta(hours=1)
    z=summarize([row(n=300,events=1,directional=300,correct=270)],
                now=now,sources_visible=True)
    assert z["status"]=="FUTURE_WINDOW_CLOSED_UNATTESTED"
    assert z["cards"][0]["distinct_target_events"]==1
    assert not z["cards"][0]["descriptively_eligible"]
    assert z["profitability_proven"] is False


def test_invalid_forward_metrics_fail_closed():
    now=END
    for wrong in (
        row(n=1,events=2,directional=1,correct=1),
        row(n=3,events=2,directional=5,correct=1),
        row(score=float("nan")),
    ):
        with pytest.raises(ValueError,match="Invalid"):
            summarize([wrong],now=now,sources_visible=True)
    with pytest.raises(ValueError,match="Unbounded"):
        summarize([row() for _ in range(GROUP_LIMIT+2)],
                  now=now,sources_visible=True)

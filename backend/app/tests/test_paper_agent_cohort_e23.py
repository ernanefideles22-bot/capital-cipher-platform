"""E2.3 SQL read-only rolling-window skill cohorts, no trading actions."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.selectable import Select

from app.strategy.paper_agent_cohort_e23 import (
    cohort_query, summarize_cohort_rows, GROUP_LIMIT,
)

NOW = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)


def row(agent="TraderA", *, settled=35, directional=30, correct=19,
        brier=.21, neutral=.25, version="v1", role="SHADOW"):
    return SimpleNamespace(
        agent_name=agent, agent_version=version,
        decision_role=role, symbol="BTCUSDT", timeframe="15m",
        settled=settled, directional=directional, correct=correct,
        mean_brier=brier, mean_neutral_brier=neutral,
    )


def test_sql_select_is_bounded_grouped_and_uses_time_matched_realizations():
    stmt = cohort_query(NOW)
    assert isinstance(stmt, Select)
    sql = str(stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert "SELECT " in sql and " GROUP BY " in sql and " ORDER BY " in sql
    assert "agent_forecasts" in sql
    assert "agent_forecast_outcomes" in sql
    assert "realized_at" in sql and "target_at" in sql
    assert "created_at" in sql and "2026-10-25" in sql
    assert "LIMIT 901" in sql


def test_true_30_sample_card_no_promotion_or_pnl_claim():
    z = summarize_cohort_rows([row()], tables_visible=True, now=NOW)
    assert z["status"] == "BOUNDED_AGGREGATE"
    assert z["descriptive_groups"] == 1
    assert not z["agent_skill_or_profitability_proven"]
    assert not z["prospective_study_started"]
    card = z["cards"][0]
    assert card["settled"] == 35
    assert card["directional_accuracy"] == round(19/30, 6)
    assert card["brier_advantage"] == .04
    assert card["sufficient_for_descriptive_metrics"]
    assert not card["eligible_for_promotion"]


def test_bounded_groups_truncate_explicitly_without_false_total():
    items = [row(agent=f"Agent{i:04d}", settled=2)
             for i in range(GROUP_LIMIT + 1)]
    z = summarize_cohort_rows(items, tables_visible=True, now=NOW)
    assert z["groups_truncated"]
    assert not z["group_counts_are_complete"]
    assert z["groups_scanned"] == GROUP_LIMIT
    assert z["descriptive_groups"] == 0
    assert len(z["cards"]) == 25
    assert z["cards_truncated"]


def test_missing_table_not_confused_with_zero_agent_skill():
    z = summarize_cohort_rows([], tables_visible=False, now=NOW)
    assert z["status"] == "SOURCE_NOT_VISIBLE"
    assert z["groups_scanned"] is None
    assert z["descriptive_groups"] is None
    with pytest.raises(ValueError, match="invisible"):
        summarize_cohort_rows([row()], tables_visible=False, now=NOW)


def test_invalid_statistics_and_duplicates_fail_closed():
    with pytest.raises(ValueError, match="Invalid aggregate"):
        summarize_cohort_rows([row(settled=1, directional=2)],
                              tables_visible=True, now=NOW)
    with pytest.raises(ValueError, match="Invalid aggregate"):
        summarize_cohort_rows([row(brier=float("nan"))],
                              tables_visible=True, now=NOW)
    with pytest.raises(ValueError, match="Duplicate group"):
        summarize_cohort_rows([row(), row()],
                              tables_visible=True, now=NOW)
    with pytest.raises(ValueError, match="Unbounded"):
        summarize_cohort_rows([row(agent=f"A{i}") for i in
                               range(GROUP_LIMIT + 2)],
                              tables_visible=True, now=NOW)

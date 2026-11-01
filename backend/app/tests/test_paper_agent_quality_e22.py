"""E2.2 calibrated forecast preflight: immutable, read-only PAPER evidence."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.strategy.paper_agent_quality_e22 import evaluate_runtime_quality

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def forecast(i="one", *, agent="TraderA", prob=.8, role="SHADOW",
             timestamp=None, target=None, created=None):
    start = timestamp or NOW
    return SimpleNamespace(
        forecast_id=i, agent_name=agent, agent_version="v1",
        decision_role=role, symbol="BTCUSDT", timeframe="15m",
        signal="BUY" if prob > .5 else "SELL",
        probability_up=prob, created_at=created or start + timedelta(seconds=1),
        forecast_at=start, target_at=target or start + timedelta(minutes=15),
    )


def outcome(i="one", *, actual=1., timestamp=None):
    return SimpleNamespace(
        forecast_id=i, realized_up=actual,
        realized_at=timestamp or NOW + timedelta(minutes=15),
    )


def job(i="job-1", *, role="SHADOW", state="COMPLETED", agent="TraderA"):
    return SimpleNamespace(
        execution_id=i, agent_name=agent, agent_version="v1",
        decision_role=role, status=state,
    )


def run(forecasts, outcomes, jobs=(), *, all_visible=True):
    return evaluate_runtime_quality(
        forecasts, outcomes, jobs, forecasts_truncated=False,
        jobs_truncated=False,
        forecast_table_visible=all_visible,
        outcome_table_visible=all_visible,
        jobs_table_visible=all_visible,
    )


def test_read_only_brier_and_directional_calibration_not_trade_profit():
    f=[forecast("up",prob=.8), forecast("down",prob=.2, role="PRIMARY")]
    o=[outcome("up", actual=1.), outcome("down", actual=0.)]
    x=run(f,o,[job(),job("job-2",role="PRIMARY")])
    assert x["mode"] == "READ_ONLY_HISTORICAL_AGENT_FORECAST_PREFLIGHT"
    assert x["agent_profitability_proven"] is False
    assert x["complete_300_agent_coverage_confirmed"] is False
    assert x["job_coverage"]["distinct_agent_names"] == 1
    assert x["job_coverage"]["jobs_by_role"] == {"PRIMARY":1,"SHADOW":1}
    assert x["forecasts"]["settled_count"] == 2
    assert x["forecasts"]["descriptively_eligible_cards"] == 0
    assert x["forecasts"]["distinct_settled_agent_version_universes"] == 2
    assert all(abs(z["mean_brier"]-.04) < 1e-7 for z in
               x["forecasts"]["agent_version_quality_cards"])
    assert all(z["directional_accuracy"] == 1. for z in
               x["forecasts"]["agent_version_quality_cards"])
    assert all(not z["strategy_or_weight_promotion_authorized"] for z in
               x["forecasts"]["agent_version_quality_cards"])


def test_no_outcome_is_unsettled_not_false_failure():
    x=run([forecast("notyet")],[])
    assert x["forecasts"]["settled_count"] == 0
    assert x["forecasts"]["unsettled_count"] == 1
    assert x["forecasts"]["agent_version_quality_cards"] == []


def test_invalid_future_creation_or_outcome_time_excluded():
    invalid=forecast("late",created=NOW+timedelta(minutes=16))
    wrong=forecast("wrong")
    x=run([invalid,wrong],
          [outcome("late"),outcome("wrong",
                                  timestamp=NOW+timedelta(minutes=16))])
    assert x["forecasts"]["settled_count"] == 0
    assert x["forecasts"]["invalid_timing_count"] == 2


def test_ties_are_not_counted_as_directional_accuracy():
    z=run([forecast("neutral",prob=.5)],
          [outcome("neutral",actual=.5)])
    card=z["forecasts"]["agent_version_quality_cards"][0]
    assert card["directional_forecasts_settled"] == 0
    assert card["directional_accuracy"] is None
    assert card["mean_brier"] == 0


def test_30_forecasts_required_for_descriptive_card_only():
    fs=[forecast(str(i),prob=.8,
                 timestamp=NOW+timedelta(hours=i)) for i in range(30)]
    os=[outcome(str(i),actual=1.,
                 timestamp=NOW+timedelta(hours=i,minutes=15)) for i in range(30)]
    z=run(fs,os)
    card=z["forecasts"]["agent_version_quality_cards"][0]
    assert card["settled_forecasts"] == 30
    assert card["sufficient_for_descriptive_card"] is True
    assert card["strategy_or_weight_promotion_authorized"] is False
    assert z["agent_profitability_proven"] is False


def test_duplicate_forecast_or_outcome_cannot_inflate_sample():
    f=[forecast("same"),forecast("same")]
    with pytest.raises(ValueError,match="Duplicate forecast"):
        run(f,[])
    with pytest.raises(ValueError,match="Duplicate realized"):
        run([forecast("same")],[outcome("same"),outcome("same")])
    with pytest.raises(ValueError,match="Duplicate runtime"):
        run([],[],[job(),job()])


def test_missing_table_is_not_misrepresented_as_zero_forecasts():
    z=run([],[],all_visible=False)
    assert z["forecasts"]["settled_count"] is None
    assert z["forecasts"]["status"] == "FORECAST_OR_OUTCOME_TABLE_NOT_VISIBLE"
    assert z["job_coverage"]["status"] == "TABLE_NOT_VISIBLE_OR_ABSENT"
    with pytest.raises(ValueError,match="not readable"):
        run([],[],[job()],all_visible=False)


def test_an_outcome_without_visible_forecast_fails_closed():
    with pytest.raises(ValueError,match="without visible forecast"):
        run([],[outcome("absent")])

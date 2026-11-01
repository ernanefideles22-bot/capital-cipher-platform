"""E2.4: independent market event census and fail-closed candle provenance tests."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

from app.strategy.paper_market_provenance_e24 import (
    independence_query, event_probe_query, summarize_market_evidence,
)

AT = datetime(2026, 11, 1, 1, 15, tzinfo=timezone.utc)


def market(*, n=300, targets=1, correlations=1, agents=300):
    return SimpleNamespace(
        symbol="BTCUSDT", timeframe="15m", settled=n,
        distinct_target_events=targets, distinct_correlations=correlations,
        distinct_agent_names=agents,
        oldest_target_at=AT-timedelta(minutes=30),
        newest_target_at=AT-timedelta(minutes=15),
    )


def event(*, first=None, outcome=None, price=100):
    return SimpleNamespace(
        symbol="BTCUSDT", timeframe="15m",
        forecast_at=AT-timedelta(minutes=30),
        target_at=AT-timedelta(minutes=15),
        forecast_count=300, reference_min=price,
        reference_max=price, outcome_min=price+1,
        outcome_max=price+1,
        first_forecast_created=first or AT-timedelta(minutes=29),
        first_outcome_created=outcome or AT-timedelta(minutes=14),
    )


def candle(closed, price, *, received=None, recorded=None, exchange="BINANCE"):
    receipt=received or closed+timedelta(seconds=1)
    return SimpleNamespace(
        symbol="BTCUSDT", timeframe="15m",
        closed_at=closed, close=price, exchange=exchange,
        received_at=receipt, recorded_at=recorded or receipt,
        quality_status="ACCEPTED",
    )


def test_aggregate_sql_is_select_and_counts_distinct_candle_targets():
    for query in (independence_query, event_probe_query):
        sql=query(AT)
        assert isinstance(sql,Select)
        compiled=str(sql.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds":True},
        ))
        assert " JOIN " in compiled and " GROUP BY " in compiled
        assert "agent_forecasts" in compiled and "agent_forecast_outcomes" in compiled
        assert "2026-10-25" in compiled
    compiled=str(independence_query(AT).compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds":True},
    ))
    assert "distinct(" in compiled.lower() and ".target_at)" in compiled.lower()


def test_three_hundred_agents_share_one_market_target_not_three_hundred():
    ev=event()
    ref=candle(ev.forecast_at,100)
    target=candle(ev.target_at,101)
    result=summarize_market_evidence([market()],[ev],[ref,target],
                                      observation_table_visible=True,now=AT)
    summary=result["independence"]
    assert summary["total_settled_forecasts"]==300
    assert summary["distinct_series_target_events"]==1
    assert summary["forecasts_per_distinct_series_target_event"]==300
    assert result["observation_probe"]["counts"][
        "reference_and_target_matching_same_exchange"
    ]==1
    assert result["observation_probe"]["matching_exchange_labels"]=={
        "BINANCE":1
    }
    assert result["external_exchange_source_authenticated"] is False
    assert result["profitability_or_agent_promotion_authorized"] is False


def test_matching_price_but_arrived_after_forecast_is_not_asof_evidence():
    ev=event()
    late_ref=candle(ev.forecast_at,100,
                    received=AT-timedelta(minutes=28))
    target=candle(ev.target_at,101)
    result=summarize_market_evidence(
        [market()],[ev],[late_ref,target],
        observation_table_visible=True,now=AT,
    )
    counts=result["observation_probe"]["counts"]
    assert counts["reference_observation_time_exists"]==1
    assert counts.get("reference_price_and_asof_recorded_match",0)==0
    assert counts.get("reference_and_target_matching_same_exchange",0)==0
    assert counts["price_or_asof_provenance_gap"]==1


def test_price_mismatch_and_exchange_ambiguity_stay_visible():
    ev=event()
    incompatible=summarize_market_evidence(
        [market()],[ev],[
            candle(ev.forecast_at,99),candle(ev.target_at,101)
        ],observation_table_visible=True,now=AT,
    )
    assert incompatible["observation_probe"]["counts"][
        "price_or_asof_provenance_gap"
    ]==1
    ambiguous=summarize_market_evidence(
        [market()],[ev],[
            candle(ev.forecast_at,100,exchange="BINANCE"),
            candle(ev.target_at,101,exchange="BINANCE"),
            candle(ev.forecast_at,100,exchange="GATE"),
            candle(ev.target_at,101,exchange="GATE"),
        ],observation_table_visible=True,now=AT,
    )
    assert ambiguous["observation_probe"]["counts"][
        "multiple_exchanges_match_same_event"
    ]==1
    assert ambiguous["external_exchange_source_authenticated"] is False


def test_invisible_and_truncated_source_never_pretend_complete():
    hidden=summarize_market_evidence(
        [market()],[event()],[],observation_table_visible=False,now=AT,
    )
    assert hidden["observation_probe"]["status"]=="TABLE_NOT_VISIBLE_OR_NOT_AUTHORIZED"
    assert hidden["independence"]["total_settled_forecasts"]==300
    with pytest.raises(ValueError,match="Invisible"):
        summarize_market_evidence(
            [market()],[event()],[candle(event().forecast_at,100)],
            observation_table_visible=False,now=AT,
        )
    with pytest.raises(ValueError,match="cardinality"):
        summarize_market_evidence(
            [market(n=2,targets=3)],[],[],
            observation_table_visible=True,now=AT,
        )



def test_candle_receipt_before_forecast_but_storage_after():
    ev = event()
    ref = candle(ev.forecast_at, 100)
    ref.recorded_at = ev.first_forecast_created + timedelta(seconds=5)
    target = candle(ev.target_at, 101)
    r = summarize_market_evidence(
        [market()], [ev], [ref, target],
        observation_table_visible=True, now=AT,
    )
    c = r["observation_probe"]["counts"]
    assert c["reference_price_match_ignore_timing"] == 1
    assert c["reference_receipt_before_creation"] == 1
    assert c["reference_stored_after_creation"] == 1
    assert c.get("reference_asof_price_and_time_match", 0) == 0
    assert c["target_asof_price_and_time_match"] == 1


def test_candle_price_mismatch_does_not_become_clock_error():
    ev = event()
    r = summarize_market_evidence(
        [market()], [ev],
        [candle(ev.forecast_at, 99), candle(ev.target_at, 101)],
        observation_table_visible=True, now=AT,
    )
    c = r["observation_probe"]["counts"]
    assert c["reference_price_mismatch"] == 1
    assert c["target_asof_price_and_time_match"] == 1
    assert c.get("reference_receipt_too_late_or_clock_invalid", 0) == 0


def test_naive_clock_rejected():
    with pytest.raises(ValueError,match="clock"):
        independence_query(datetime(2026,11,1))

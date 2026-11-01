"""E2.6 stored Binance-labelled candles: no writes, no strategy execution."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.strategy.paper_future_market_readiness_e26 import (
    WARMUP, recent_warmup_query, summarize_warmup,
)

NOW=datetime(2026,11,1,6,0,tzinfo=timezone.utc)


def candle(i:int, *, n=WARMUP, timeframe="15m", received_offset=2,
           stored_offset=3, quality="ACCEPTED"):
    step=timedelta(minutes=15)
    close=NOW-(n-i)*step
    price=100+i*.01
    return SimpleNamespace(
        exchange="BINANCE",symbol="BTCUSDT",timeframe=timeframe,
        closed_at=close,
        received_at=close+timedelta(seconds=received_offset),
        recorded_at=close+timedelta(seconds=stored_offset),
        quality_status=quality,
        open=price,high=price+1,low=price-1,close=price+.3,
        volume=101,
    )


def test_sql_only_select_is_ordered_and_limited_to_300_rows():
    q=recent_warmup_query("BTCUSDT","15m",now=NOW)
    sql=str(q.compile(dialect=postgresql.dialect(),
                      compile_kwargs={"literal_binds":True})).lower()
    assert "select" in sql and "candle_observations" in sql
    assert "order by" in sql and "limit 300" in sql
    assert "binance" in sql and "btcusdt" in sql
    for bad in (("BTCUSDT","4h"),("ETHUSDT","15m")):
        with pytest.raises(ValueError,match="Market not"):
            recent_warmup_query(*bad,now=NOW)


def test_exact_300_recent_candles_is_only_possible_internal_source():
    bars=[candle(i) for i in range(WARMUP)]
    result=summarize_warmup(bars,symbol="BTCUSDT",timeframe="15m",
                            now=NOW,table_visible=True)
    assert result["warmup_complete"]
    assert result["gap_count"]==0
    assert result["recorded_within_one_interval"]==WARMUP
    assert result["status"]=="POSSIBLE_INTERNAL_CANDLE_FEED_FOR_FUTURE_STUDY"
    assert not result["external_exchange_authenticated"]
    assert not result["persistent_future_collector_running"]
    assert not result["automated_strategy_promotion_authorized"]


def test_gaps_quality_and_stale_feed_do_not_qualify():
    bars=[candle(i) for i in range(WARMUP)]
    bars.pop(30)
    x=summarize_warmup(bars,symbol="BTCUSDT",timeframe="15m",
                       now=NOW,table_visible=True)
    assert not x["warmup_complete"]
    assert x["gap_count"]==1
    assert x["status"]=="INTERNAL_CANDLE_FEED_NOT_YET_VALIDATED"
    bars=[candle(i,quality="WARNING" if i==50 else "ACCEPTED")
          for i in range(WARMUP)]
    y=summarize_warmup(bars,symbol="BTCUSDT",timeframe="15m",
                       now=NOW,table_visible=True)
    assert y["poor_quality_count"]==1
    assert y["status"]=="INTERNAL_CANDLE_FEED_NOT_YET_VALIDATED"
    with pytest.raises(ValueError,match="Duplicate"):
        summarize_warmup([candle(0),candle(0)],
                         symbol="BTCUSDT",timeframe="15m",
                         now=NOW,table_visible=True)


def test_preflight_never_calls_missing_table_zero_success():
    z=summarize_warmup([],symbol="BTCUSDT",timeframe="15m",
                       now=NOW,table_visible=False)
    assert z["status"]=="TABLE_NOT_VISIBLE"
    assert z["sampled_candles"]==0
    assert z["warmup_complete"] if "warmup_complete" in z else True
    with pytest.raises(ValueError,match="Invisible"):
        summarize_warmup([candle(0)],symbol="BTCUSDT",
                         timeframe="15m",now=NOW,table_visible=False)


def test_out_of_order_returned_rows_still_check_continuity_without_fabrication():
    bars=[candle(i) for i in reversed(range(WARMUP))]
    x=summarize_warmup(bars,symbol="BTCUSDT",
                       timeframe="15m",now=NOW,table_visible=True)
    assert x["warmup_complete"]
    assert x["sampled_candles"]==300

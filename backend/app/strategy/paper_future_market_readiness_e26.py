"""E2.6 storage-readiness preflight for an existing PAPER candle pipeline.

Bounded SELECT ONLY: at most 300 recent rows for each preregistered
market/timeframe. Presence of stored candle rows does NOT authenticate
a venue or a truly independent forward collector.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite

from sqlalchemy import desc, select

from app.database.models import CandleObservationModel as C
from app.strategy.candidate_replay import BAR_SECONDS
from app.strategy.prospective_core_gate_e26 import CAMPAIGN, MARKETS

WARMUP = CAMPAIGN["warmup_candles_per_market"]
EXPECTED_EXCHANGE = "BINANCE"


def recent_warmup_query(symbol: str, timeframe: str, *, now: datetime):
    if (symbol,timeframe) not in MARKETS:
        raise ValueError("Market not in frozen forward study")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Aware clock required")
    cutoff=now.astimezone(timezone.utc)
    return (
        select(C)
        .where(C.exchange == EXPECTED_EXCHANGE,
               C.symbol == symbol, C.timeframe == timeframe,
               C.closed_at <= cutoff)
        .order_by(desc(C.closed_at))
        .limit(WARMUP)
    )


def summarize_warmup(rows, *, symbol: str, timeframe: str,
                     now: datetime, table_visible: bool) -> dict:
    if (symbol,timeframe) not in MARKETS:
        raise ValueError("Market not in frozen forward study")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Aware clock required")
    if len(rows) > WARMUP:
        raise ValueError("Warmup rows exceed cap")
    result={
        "symbol":symbol,"timeframe":timeframe,"exchange_label":EXPECTED_EXCHANGE,
        "mode":"E26_EXISTING_STAGING_MARKET_DATA_READ_ONLY_WARMUP",
        "required_warmup_candles":WARMUP,
        "sampled_candles":len(rows),
        "source_visible":bool(table_visible),
        "external_exchange_authenticated":False,
        "persistent_future_collector_running":False,
        "automated_strategy_promotion_authorized":False,
        "future_study_confirmed_profitable":False,
    }
    if not table_visible:
        if rows:
            raise ValueError("Invisible observation source has rows")
        result["status"]="TABLE_NOT_VISIBLE"
        return result
    if not rows:
        result["status"]="NO_VISIBLE_MARKET_DATA"
        return result
    now=now.astimezone(timezone.utc)
    sorted_rows=sorted(rows,key=lambda x:x.closed_at)
    step=timedelta(seconds=BAR_SECONDS[timeframe])
    seen=set()
    gaps=0
    timely_received=0
    timely_recorded=0
    invalid_times=0
    poor_quality=0
    for i,ob in enumerate(sorted_rows):
        closed=ob.closed_at.astimezone(timezone.utc)
        received=ob.received_at.astimezone(timezone.utc)
        recorded=ob.recorded_at.astimezone(timezone.utc)
        if ob.closed_at.tzinfo is None or ob.received_at.tzinfo is None or ob.recorded_at.tzinfo is None:
            raise ValueError("Naive observation clock")
        if (closed in seen or ob.exchange != EXPECTED_EXCHANGE or
            ob.symbol != symbol or ob.timeframe != timeframe):
            raise ValueError("Duplicate/mismatched market candle")
        seen.add(closed)
        if i and closed - sorted_rows[i-1].closed_at.astimezone(timezone.utc)!=step:
            gaps+=1
        try:
            values=tuple(float(getattr(ob,k)) for k in ("open","high","low","close","volume"))
        except (ValueError,TypeError,OverflowError) as ex:
            raise ValueError("Invalid stored OHLCV") from ex
        o,h,l,c,v=values
        if (not all(isfinite(x) for x in values) or
            min(o,h,l,c)<=0 or v<0 or
            h < max(o,l,c) or l > min(o,h,c)):
            raise ValueError("Invalid OHLCV stored candle")
        if str(ob.quality_status) not in {"ACCEPTED","VALID"}:
            poor_quality+=1
        if received<closed or recorded<closed or received>now or recorded>now:
            invalid_times+=1
        if closed<=received<=closed+step:
            timely_received+=1
        if closed<=recorded<=closed+step:
            timely_recorded+=1
    latest=sorted_rows[-1].closed_at.astimezone(timezone.utc)
    newest_allowed=now-step*2
    warmup_complete=(len(rows)==WARMUP and gaps==0)
    recent_enough=latest>=newest_allowed
    result.update({
        "first_candle_close_utc":sorted_rows[0].closed_at.astimezone(timezone.utc).isoformat(),
        "last_candle_close_utc":latest.isoformat(),
        "gap_count":gaps,"poor_quality_count":poor_quality,
        "invalid_clock_count":invalid_times,
        "observed_within_one_interval":timely_received,
        "recorded_within_one_interval":timely_recorded,
        "warmup_complete":warmup_complete,
        "last_candle_recent_within_two_intervals":recent_enough,
        "status":"POSSIBLE_INTERNAL_CANDLE_FEED_FOR_FUTURE_STUDY"
                 if (warmup_complete and recent_enough and not poor_quality
                     and not invalid_times and timely_recorded==len(rows))
                 else "INTERNAL_CANDLE_FEED_NOT_YET_VALIDATED",
    })
    return result

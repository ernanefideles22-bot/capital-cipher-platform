"""E2.6 campaign contract safety and anti-lookahead regression tests.

Synthetic future timestamps are UNIT TEST INPUTS, never trading evidence.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.strategy.prospective_core_gate_e26 import (
    ALLOWED_SOURCE, CAMPAIGN, HEADER, campaign_sha256,
    load_causally_observed_candles, expected_evaluation_count,
)
from scripts.run_e26_future_gate_evaluation import evaluate


def make_file(path: Path, *, tf="4h", tamper=None):
    step={"15m":timedelta(minutes=15),"1h":timedelta(hours=1),
          "4h":timedelta(hours=4)}[tf]
    start=datetime.fromisoformat(
        CAMPAIGN["first_evaluation_candle_closed_at_utc"]
    )
    count=CAMPAIGN["warmup_candles_per_market"]+expected_evaluation_count(tf)
    rows=[]
    for i in range(count):
        closed=start+(i-CAMPAIGN["warmup_candles_per_market"])*step
        opened=closed-step
        px=100+i*.01
        rows.append({
            "open_time_ms":int(opened.timestamp()*1000),
            "open":px,"high":px+1,"low":px-1,"close":px+.3,
            "volume":100,
            "observed_at_utc":(closed+timedelta(seconds=1)).isoformat(),
            "source":ALLOWED_SOURCE,
        })
    if tamper:
        tamper(rows)
    with path.open("w") as f:
        writer=csv.DictWriter(f,fieldnames=HEADER.split(","),
                              lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_future_protocol_is_frozen_and_not_automatically_trading():
    assert len(campaign_sha256())==64
    assert CAMPAIGN["continuous_collection_confirmed"] is False
    assert CAMPAIGN["collection_job_enabled"] is False
    assert CAMPAIGN["real_money_authorized"] is False
    assert CAMPAIGN["automatic_promotion_authorized"] is False
    assert CAMPAIGN["e25_protocol_sha256"]
    assert expected_evaluation_count("15m")==2880
    assert expected_evaluation_count("1h")==720
    assert expected_evaluation_count("4h")==180


def test_future_candles_require_real_observation_field_and_exact_period(tmp_path):
    p=make_file(tmp_path/"observed.csv")
    now=datetime(2026,12,5,5,tzinfo=timezone.utc)
    bars,source=load_causally_observed_candles(
        p,symbol="SOLUSDT",timeframe="4h",now=now,
    )
    assert len(bars)==480
    assert source["evaluation_candles"]==180
    assert source["receipt_times_independently_attested"] is False
    assert source["source_provenance"]=="CSV_DECLARED_UNVERIFIED"
    assert bars[300].closed_at==datetime(
        2026,11,5,tzinfo=timezone.utc,
    )
    assert bars[-1].closed_at==datetime(
        2026,12,4,20,tzinfo=timezone.utc,
    )


def test_cannot_peek_before_campaign_end_even_with_synthetic_future_rows(tmp_path):
    p=make_file(tmp_path/"fake-future.csv")
    with pytest.raises(ValueError,match="not ended"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",
            now=datetime(2026,11,1,tzinfo=timezone.utc),
        )


def test_source_and_bar_latency_fail_closed(tmp_path):
    now=datetime(2026,12,5,5,tzinfo=timezone.utc)
    p=make_file(
        tmp_path/"wrong-source.csv",
        tamper=lambda rows: rows[300].update(source="SYNTHETIC"),
    )
    with pytest.raises(ValueError,match="synthetic"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",now=now,
        )
    def make_late(rows):
        arrival=datetime.fromisoformat(rows[300]["observed_at_utc"])
        rows[300]["observed_at_utc"]=(arrival+timedelta(hours=8)).isoformat()
    p=make_file(tmp_path/"late.csv",tamper=make_late)
    with pytest.raises(ValueError,match="Late/backfilled"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",now=now,
        )


def test_time_travel_duplicate_and_missing_bar_fail_closed(tmp_path):
    now=datetime(2026,12,5,5,tzinfo=timezone.utc)
    def wrong_clock(rows):
        closed=datetime.fromisoformat(rows[300]["observed_at_utc"])
        rows[300]["observed_at_utc"]=(closed-timedelta(minutes=2)).isoformat()
    p=make_file(tmp_path/"early.csv",tamper=wrong_clock)
    with pytest.raises(ValueError,match="observed before close"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",now=now,
        )
    p=make_file(tmp_path/"missing.csv")
    lines=p.read_text().splitlines()
    p.write_text("\n".join(lines[:-1])+"\n")
    with pytest.raises(ValueError,match="exact warmup"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",now=now,
        )
    def duplicate(rows):
        rows[320]["open_time_ms"]=rows[319]["open_time_ms"]
    p=make_file(tmp_path/"dupe.csv",tamper=duplicate)
    with pytest.raises(ValueError,match="Gap, overlap"):
        load_causally_observed_candles(
            p,symbol="SOLUSDT",timeframe="4h",now=now,
        )


@pytest.mark.asyncio
async def test_future_evaluator_cannot_run_on_unfinished_window(tmp_path):
    with pytest.raises(ValueError,match="has not completed"):
        await evaluate(
            tmp_path,now=datetime(2026,11,1,tzinfo=timezone.utc),
        )

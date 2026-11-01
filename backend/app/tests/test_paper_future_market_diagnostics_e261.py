"""E2.6.1 regression: explain bad historical rows without relaxing gates."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.strategy.paper_future_market_diagnostics_e261 import (
    MAX_EXAMPLES, diagnose_candle_rows,
)
from app.strategy.paper_future_market_readiness_e26 import summarize_warmup

NOW = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)


def candle(i, *, symbol="BTCUSDT", timeframe="15m", count=300,
           quality="VALID", warnings=(), errors=(),
           receipt_seconds=1, persist_seconds=2):
    steps = {"15m": 900, "1h": 3600, "4h": 14400}
    close_at = NOW - timedelta(seconds=(count-i)*steps[timeframe])
    return SimpleNamespace(
        exchange="BINANCE", symbol=symbol, timeframe=timeframe,
        closed_at=close_at,
        received_at=close_at+timedelta(seconds=receipt_seconds),
        recorded_at=close_at+timedelta(seconds=persist_seconds),
        quality_status=quality, quality_warnings=list(warnings),
        quality_errors=list(errors), quality_score=80,
        open=100, high=101, low=99, close=100.5, volume=5,
    )


def diagnose(rows, symbol="BTCUSDT", timeframe="15m"):
    return diagnose_candle_rows(
        rows, symbol=symbol, timeframe=timeframe, now=NOW,
    )


def test_historical_quality_codes_and_late_persistence_are_not_live_proof():
    bars = [candle(i) for i in range(300)]
    bars[0] = candle(0, quality="WARNING", warnings=["STALE_DATA"],
                     receipt_seconds=2, persist_seconds=1000)
    bars[299] = candle(299, quality="SUSPECT",
                       warnings=["DATA_ANOMALY"], errors=["BAD_RANGE"],
                       receipt_seconds=1000, persist_seconds=1001)
    result = diagnose(bars)
    assert result["quality_status_counts"] == {
        "SUSPECT": 1, "VALID": 298, "WARNING": 1,
    }
    assert result["warning_code_candle_counts"] == {
        "DATA_ANOMALY": 1, "STALE_DATA": 1,
    }
    assert result["error_code_candle_counts"] == {"BAD_RANGE": 1}
    assert result["late_receipt_rows"] == 1
    assert result["late_persistence_rows"] == 2
    assert result["late_persistence_with_timely_receipt_rows"] == 1
    assert result["recent_window_unaccepted_quality_rows"] == 1
    assert result["recent_window_untimely_receipt_rows"] == 1
    assert result["recent_window_untimely_persistence_rows"] == 1
    assert result["historical_backfill_vs_live_delay"].startswith("CANNOT_DETERMINE")
    assert not result["source_externally_authenticated"]
    assert not result["collector_prospectively_attested"]
    assert not result["can_promote_strategy"]
    assert not result["execution_authorized"]
    # E2.6 existing status gate MUST NOT turn a WARNING into valid history.
    prior = summarize_warmup(
        bars, symbol="BTCUSDT", timeframe="15m", now=NOW,
        table_visible=True,
    )
    assert prior["status"] == "INTERNAL_CANDLE_FEED_NOT_YET_VALIDATED"
    assert prior["poor_quality_count"] == 2


def test_older_late_rows_do_not_misrepresent_recent_24h_quality():
    bars = [candle(i) for i in range(300)]
    bars[0] = candle(0, receipt_seconds=1700, persist_seconds=1800)
    r = diagnose(bars)
    assert r["late_receipt_rows"] == 1
    assert r["late_persistence_rows"] == 1
    assert r["recent_window_untimely_receipt_rows"] == 0
    assert r["recent_window_untimely_persistence_rows"] == 0
    assert r["recent_window_rows"] == 96
    assert r["warmup_shortfall_rows"] == 0


@pytest.mark.parametrize("symbol,timeframe,count,missing", [
    ("ETHUSDT", "1h", 285, 15),
    ("SOLUSDT", "4h", 206, 94),
])
def test_short_warmup_is_reported_not_relabelled_as_complete(
        symbol, timeframe, count, missing):
    rows = [candle(i,symbol=symbol,timeframe=timeframe,count=count)
            for i in range(count)]
    r=diagnose(rows,symbol=symbol,timeframe=timeframe)
    assert r["warmup_shortfall_rows"] == missing
    assert r["sampled_rows"] == count
    assert not r["readiness_gate_modified"]


def test_sanitized_bounded_examples_and_no_raw_sensitive_warnings():
    rows = [candle(i,quality="WARNING",
                   warnings=["invalid secret=123", "STALE_DATA",
                             "STALE_DATA"]) for i in range(300)]
    r=diagnose(rows)
    assert len(r["quality_examples_newest_first"]) == MAX_EXAMPLES
    assert r["warning_code_candle_counts"] == {
        "STALE_DATA": 300, "UNRECOGNIZED_CODE": 300,
    }
    assert "secret=123" not in str(r)
    assert r["quality_examples_newest_first"][0]["closed_at_utc"] > (
        r["quality_examples_newest_first"][-1]["closed_at_utc"])


def test_reject_wrong_market_duplicate_naive_clock_and_unbounded_rows():
    with pytest.raises(ValueError,match="Market not"):
        diagnose_candle_rows([],symbol="BTCUSDT",timeframe="1h",now=NOW)
    with pytest.raises(ValueError,match="Unbounded"):
        diagnose([candle(i) for i in range(301)])
    with pytest.raises(ValueError,match="Duplicate"):
        diagnose([candle(10),candle(10)])
    bad=candle(1)
    bad.recorded_at=datetime(2026,11,1,5)
    with pytest.raises(ValueError,match="Naive"):
        diagnose([bad])
    with pytest.raises(ValueError,match="clock"):
        diagnose_candle_rows([],symbol="BTCUSDT",timeframe="15m",
                             now=datetime(2026,11,1,6))


def test_absent_rows_do_not_claim_prospective_collection():
    r=diagnose([])
    assert r["sampled_rows"] == 0
    assert r["warmup_shortfall_rows"] == 300
    assert r["recent_window_rows"] == 0
    assert r["quality_status_counts"] == {}
    assert not r["source_externally_authenticated"]

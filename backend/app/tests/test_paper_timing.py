"""Timing observations cannot manufacture fills, identities or zero samples."""
import asyncio
from collections import deque

import pytest

from app.office.paper_timing import MILESTONES, PaperTiming, mark_paper_timing, paper_timing, timing_snapshot


def completed_trace(timeframe="15m"):
    ticks = iter(i / 100 for i in range(20))
    trace = PaperTiming("BTCUSDT", timeframe, clock=lambda: next(ticks))
    for name in MILESTONES[1:]: trace.mark(name, correlation_id="correlation", decision_id="decision",
                                        strategy="SCALP_15M_v1", paper_order_id="order" if name == "paper_fill_committed" else None)
    return trace.finish()


def test_measured_fill_uses_clock_offsets_and_does_not_expose_order_payload():
    row = completed_trace()
    assert row["milestones_ms"]["paper_fill_committed"] == 60
    assert "entry_price" not in row and "position_size" not in row
    result = timing_snapshot([row])
    assert result["receipt_to_fill_ms"] == {"samples": 1, "p50": 60, "p95": 60, "p99": 60}
    assert result["by_timeframe"]["1h"]["p95"] is None


def test_no_fill_failure_is_not_a_zero_latency_sample():
    trace = PaperTiming("BTCUSDT", "15m")
    row = trace.finish(error_code="DATA_QUALITY_ERROR")
    result = timing_snapshot([row])
    assert row["outcome"] == "FAILED"
    assert result["receipt_to_fill_ms"] == {"samples": 0, "p50": None, "p95": None, "p99": None}


def test_cancelled_waiter_has_no_fabricated_queue_or_fill_timestamp():
    row = PaperTiming("BTCUSDT", "1h").finish(cancelled=True)
    assert row["outcome"] == "CANCELLED"
    assert "queue_acquired" not in row["milestones_ms"]
    assert timing_snapshot([row])["receipt_to_fill_ms"]["samples"] == 0


def test_postfill_error_or_cancellation_does_not_hide_the_committed_order():
    trace = PaperTiming("BTCUSDT", "15m")
    for name in MILESTONES[1:]:trace.mark(name, correlation_id="correlation", decision_id="decision",
                                       strategy="SCALP_15M_v1", paper_order_id="order" if name == "paper_fill_committed" else None)
    row = trace.finish(cancelled=True, error_code="DATABASE_ERROR")
    assert row["outcome"] == "FILLED" and row["cancelled"]
    assert row["fill_observation"] == "COMMITTED"
    assert timing_snapshot([row])["receipt_to_fill_ms"]["samples"] == 1


def test_unknown_error_text_is_not_published():
    row = PaperTiming("BTCUSDT", "15m").finish(error_code="secret-token-or-message")
    assert row["error_code"] == "OTHER"
    assert "secret-token" not in str(row)


def test_context_and_returned_snapshot_cannot_mutate_finished_trace():
    trace = PaperTiming("BTCUSDT", "15m")
    with paper_timing(trace):
        mark_paper_timing("decision", correlation_id="a", decision_id="decision-a")
        mark_paper_timing("paper_fill_committed", correlation_id="b", paper_order_id="foreign")
    row = trace.finish()
    assert "paper_fill_committed" not in row["milestones_ms"]
    trace.mark("paper_fill_committed", correlation_id="a")
    row["milestones_ms"]["forged"] = 1
    assert "forged" not in trace.finish()["milestones_ms"]
    assert "paper_fill_committed" not in trace.finish()["milestones_ms"]


async def test_concurrent_contexts_do_not_mix_correlations():
    async def one(identity):
        trace = PaperTiming("BTCUSDT", "15m")
        with paper_timing(trace):
            await asyncio.sleep(0)
            mark_paper_timing("decision", correlation_id=identity, decision_id=identity)
        return trace.finish()
    rows = await asyncio.gather(one("a"), one("b"))
    assert {row["decision_id"] for row in rows} == {"a", "b"}


def test_ring_window_is_bounded_and_returned_data_is_detached():
    rows = deque(maxlen=100)
    for _ in range(105): rows.append(completed_trace())
    result = timing_snapshot(rows)
    assert result["attempts"] == result["receipt_to_fill_ms"]["samples"] == 100
    result["recent"][0]["milestones_ms"].clear()
    assert rows[0]["milestones_ms"]


def test_fill_without_order_identity_is_not_a_fill_observation():
    trace = PaperTiming("BTCUSDT", "15m")
    for name in MILESTONES[1:]: trace.mark(name)
    row = trace.finish()
    assert timing_snapshot([row])["receipt_to_fill_ms"]["samples"] == 0
    assert row["fill_observation"] == "UNCONFIRMED"


def test_risk_block_is_explicit_and_has_no_fill_latency():
    trace = PaperTiming("BTCUSDT", "15m")
    trace.mark("risk_completed", risk_status="BLOCKED")
    row = trace.finish()
    assert row["outcome"] == "RISK_BLOCKED"
    assert timing_snapshot([row])["receipt_to_fill_ms"]["p95"] is None

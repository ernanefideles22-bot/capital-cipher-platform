"""Bounded PAPER admission timing, with one process-local monotonic clock.

Offsets start when the orchestrator receives a candle. A simulated fill is
measured after its durable approval/order transaction returns, never from the
historical candle time or order.opened_at. These samples are not venue latency.
"""
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone

MILESTONES = ("received", "queue_acquired", "decision", "risk_started",
              "risk_completed", "submit_started", "paper_fill_committed", "terminal")
ERROR_CODES = {"CLOCK_UNTRUSTED", "DATA_QUALITY_ERROR", "PRIMARY_DEADLINE_EXCEEDED",
               "DATABASE_ERROR", "AUDIT_FAILED", "RISK_ERROR"}
_active = ContextVar("paper_admission_timing", default=None)


class PaperTiming:
    def __init__(self, symbol, timeframe, *, clock=time.monotonic):
        self._clock = clock
        self._start = clock()
        self._closed = False
        self._row = {"symbol": symbol[:32], "timeframe": timeframe[:16],
                     "received_at": datetime.now(timezone.utc).isoformat(),
                     "correlation_id": None, "decision_id": None, "strategy": None,
                     "paper_order_id": None, "risk_status": None, "milestones_ms": {"received": 0.0}}

    def mark(self, name, *, correlation_id=None, decision_id=None, strategy=None, paper_order_id=None, risk_status=None):
        if self._closed or name not in MILESTONES:
            return
        if correlation_id is not None:
            existing = self._row["correlation_id"]
            if existing is not None and existing != correlation_id:
                return
            self._row["correlation_id"] = correlation_id[:64]
        for key, value in (("decision_id", decision_id), ("strategy", strategy), ("paper_order_id", paper_order_id)):
            if value is not None:
                self._row[key] = value[:128]
        if risk_status in {"APPROVED", "REDUCED", "BLOCKED", "KILL_SWITCH"}:
            self._row["risk_status"] = risk_status
        if name not in self._row["milestones_ms"]:
            elapsed = (self._clock() - self._start) * 1000
            if math.isfinite(elapsed) and elapsed >= 0:
                self._row["milestones_ms"][name] = round(elapsed, 3)

    @property
    def correlation_id(self):
        return self._row["correlation_id"]

    def finish(self, *, cancelled=False, error_code=None):
        if not self._closed:
            self.mark("terminal")
            filled = "paper_fill_committed" in self._row["milestones_ms"] and all(
                self._row[key] for key in ("correlation_id", "decision_id", "strategy", "paper_order_id"))
            blocked = self._row["risk_status"] in {"BLOCKED", "KILL_SWITCH"}
            self._row["outcome"] = "FILLED" if filled else "CANCELLED" if cancelled else "FAILED" if error_code else "RISK_BLOCKED" if blocked else "NO_FILL"
            self._row["error_code"] = error_code if error_code in ERROR_CODES else "OTHER" if error_code else None
            self._row["cancelled"] = cancelled
            self._row["fill_observation"] = (
                "COMMITTED" if filled else "UNCONFIRMED" if "submit_started" in self._row["milestones_ms"] else "NOT_SUBMITTED"
            )
            self._closed = True
        return deepcopy(self._row)


@contextmanager
def paper_timing(trace):
    token = _active.set(trace)
    try:
        yield trace
    finally:
        _active.reset(token)


def mark_paper_timing(name, **identity):
    trace = _active.get()
    if trace is not None:
        trace.mark(name, **identity)


def timing_snapshot(samples):
    """No-fill attempts never become zero-latency fill observations."""
    rows = deepcopy(list(samples))
    groups = {}
    for row in rows:
        marks = row["milestones_ms"]
        if row["outcome"] != "FILLED" or not all(key in marks for key in MILESTONES):
            continue
        values = [marks[key] for key in MILESTONES]
        if values != sorted(values):
            continue
        groups.setdefault(row["timeframe"], []).append(marks["paper_fill_committed"])
    def percentile(values, p):
        if not values:
            return None
        ordered = sorted(values)
        index = (len(ordered) - 1) * p
        low, high = math.floor(index), math.ceil(index)
        return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 3)
    all_fills = [value for values in groups.values() for value in values]
    def summary(values):
        return {"samples": len(values), **{key: percentile(values, p) for key, p in (("p50", .5), ("p95", .95), ("p99", .99))}}
    return {"scope": "orchestrator receipt to committed simulated PAPER fill; process-local rolling window",
            "durability": "volatile last 100 attempts; external snapshots may retain them",
            "attempts": len(rows), "receipt_to_fill_ms": summary(all_fills),
            "by_timeframe": {key: summary(groups.get(key, [])) for key in ("15m", "1h", "4h")},
            "recent": rows}

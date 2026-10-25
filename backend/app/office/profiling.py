"""Bounded, payload-free measurements of one reserved PRIMARY cohort.

SQL counters describe SQLAlchemy application statements, checkouts and commit calls;
they do not include driver handshakes, pre-ping or network round trips. Context
variables keep unrelated SHADOW workers out of the active trading profile.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from math import isfinite


STAGES = ("create_jobs", "requested_events", "claim_jobs", "started_events",
          "analysis", "finish_jobs", "terminal_events")
COUNTERS = ("sql_statements", "db_checkouts", "db_commit_calls")
_active = ContextVar("office_primary_profile", default=None)
_stage = ContextVar("office_primary_stage", default=None)


class PrimaryProfile:
    def __init__(self):
        self.started = time.monotonic()
        self.stages = {}

    def snapshot(self):
        return {"elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                "stages": {name: dict(values) for name, values in self.stages.items()}}


@contextmanager
def primary_profile(profile):
    token = _active.set(profile)
    try:
        yield
    finally:
        _active.reset(token)


@contextmanager
def primary_step(name):
    profile = _active.get()
    if profile is None:
        yield
        return
    if name not in STAGES:
        raise ValueError("Unknown PRIMARY measurement stage")
    row = profile.stages.setdefault(name, dict(elapsed_ms=0, calls=0,
        sql_statements=0, db_checkouts=0, db_commit_calls=0, status="RUNNING"))
    row["calls"] += 1
    token = _stage.set(name)
    start = time.monotonic()
    try:
        yield
        row["status"] = "COMPLETED"
    except BaseException as exc:
        import asyncio
        row["status"] = "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "FAILED"
        raise
    finally:
        row["elapsed_ms"] = round(row["elapsed_ms"] + (time.monotonic() - start) * 1000, 3)
        _stage.reset(token)


def record_primary_database(counter):
    profile, stage = _active.get(), _stage.get()
    if profile is not None and stage in profile.stages and counter in COUNTERS:
        profile.stages[stage][counter] += 1


def safe_primary_profile(value):
    """Allow fixed numeric counters/statuses only, including in failure logs."""
    if not isinstance(value, dict):
        return None
    def number(value):
        return round(float(value), 3) if type(value) in (int, float) and 0 <= value <= 1e12 and isfinite(value) else 0
    result = {"elapsed_ms": number(value.get("elapsed_ms")), "stages": {}}
    stages = value.get("stages")
    if not isinstance(stages, dict):
        return result
    for name in STAGES:
        row = stages.get(name)
        if not isinstance(row, dict):
            continue
        result["stages"][name] = {key: number(row.get(key)) for key in ("elapsed_ms", "calls", *COUNTERS)}
        status = row.get("status")
        result["stages"][name]["status"] = status if status in ("RUNNING", "COMPLETED", "CANCELLED", "FAILED") else "FAILED"
    return result

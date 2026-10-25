"""Reserved startup, cancellation and payload-free latency evidence."""
import asyncio
import json
from datetime import timedelta

import pytest

from app.office.isolation import IsolatedAgentRunner
from app.office.profiling import PrimaryProfile, primary_profile, primary_step, safe_primary_profile
from app.schemas.common import utcnow
from app.tests.test_office_async import FastShadow, request


async def test_ready_process_is_reused_without_running_an_agent_at_startup():
    runner = IsolatedAgentRunner(concurrency=1)
    agent = FastShadow()
    try:
        await runner.warmup()
        child, = runner._children
        assert child.is_alive() and runner.last_warmup_ms >= 0
        assert agent.total_runs == 0
        await runner.run(agent, request(agent).input)
        assert runner._children == {child} and agent.total_runs == 1
    finally:
        await runner.aclose()
    assert not runner._children


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_warmup_disposes_process_and_restores_slot(monkeypatch, cancel):
    runner = IsolatedAgentRunner(concurrency=1)
    entered = asyncio.Event()
    async def stalled(slot):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(runner, "_receive", stalled)
    task = asyncio.create_task(runner.warmup(timeout_seconds=.03 if not cancel else 5))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert not runner._children and runner._slots.qsize() == 1
    await runner.aclose()


async def test_profile_records_cancelled_phase_without_shadow_task_contamination():
    profile = PrimaryProfile()
    from app.office.profiling import record_primary_database
    async def unrelated():
        # Task was born before the PRIMARY context, as the durable worker is.
        await asyncio.sleep(.005)
        record_primary_database("sql_statements")
    task = asyncio.create_task(unrelated())
    with primary_profile(profile):
        with pytest.raises(TimeoutError):
            async def critical():
                with primary_step("create_jobs"):
                    record_primary_database("sql_statements")
                    await asyncio.sleep(1)
            await asyncio.wait_for(critical(), .02)
    await task
    row = profile.snapshot()["stages"]["create_jobs"]
    assert row["sql_statements"] == 1 and row["status"] == "CANCELLED"
    assert row["elapsed_ms"] >= 10


def test_profile_redacts_unknown_fields_invalid_numbers_and_statuses():
    result = safe_primary_profile({"elapsed_ms": float("inf"), "secret": "credential",
        "stages": {"create_jobs": {"sql_statements": 2, "db_checkouts": 10**1000,
            "elapsed_ms": "postgresql://credential", "status": "credential"},
            "postgresql://credential": {"sql_statements": 99}}})
    assert "credential" not in json.dumps(result)
    assert result["elapsed_ms"] == 0 and result["stages"]["create_jobs"]["sql_statements"] == 2


def test_fixture_emits_positive_close_after_setup_without_mutating_history():
    from app.tests.test_office_async import candles
    from app.validation.paper_flow import fresh_fixture_close
    items = candles()
    previous = items[-2]
    before = utcnow()
    old = items[-1].model_copy(update={"closed_at": before - timedelta(seconds=10)})
    closed = fresh_fixture_close(old, previous)
    assert before <= closed.closed_at <= utcnow()
    assert closed.received_at == closed.closed_at and old.closed_at < before
    assert items[-2] is previous and closed.close == old.close


def test_fixture_refuses_setup_that_would_create_a_gap():
    from app.tests.test_office_async import candles
    from app.validation.paper_flow import fresh_fixture_close
    items = candles()
    with pytest.raises(RuntimeError, match="PREPARATION_EXCEEDED_INTERVAL"):
        fresh_fixture_close(items[-1], items[-2].model_copy(update={"closed_at": utcnow() - timedelta(hours=1)}))


def test_native_failure_contains_only_current_cycle_and_safe_timings(monkeypatch):
    from app.validation.paper_flow import PaperFlowValidationError
    from app.tests.test_paper_validation_guard import gate_module
    exc = PaperFlowValidationError([{"error": "PRIMARY_DEADLINE_EXCEEDED"}], 3001,
        primary_profile={"elapsed_ms": 3000, "stages": {"started_events": {
            "elapsed_ms": 1500, "status": "CANCELLED", "credential": "sensitive"}}},
        cycle_stages={"pre_agent": 100, "credential": "sensitive", "risk": float("nan")})
    result = gate_module(monkeypatch).failure_report(exc)
    assert result["error_codes"] == ["PRIMARY_DEADLINE_EXCEEDED"]
    assert result["cycle_stages_ms"] == {"pre_agent": 100}
    assert result["primary_profile"]["stages"]["started_events"]["status"] == "CANCELLED"
    assert "sensitive" not in json.dumps(result)

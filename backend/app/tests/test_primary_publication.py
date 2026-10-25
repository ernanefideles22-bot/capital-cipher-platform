"""Ordered admission acknowledgements and failure isolation on real repositories."""
import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.core.event_bus import EventBus, Topics
from app.agents.runtime import AgentRuntime
from app.core.journal import JournalWriteResult
from app.database.models import EventJournalModel, EventOutboxModel
from app.database.repositories.repository import Repository
from app.office.profiling import safe_primary_profile
from app.schemas.events import EventTypes
from app.tests.test_office_reserved_lane import lanes  # noqa: F401


def publication_bus(runtime, *, fail=None, stall=None):
    journaled, confirmed, batches = {}, {}, []
    entered = asyncio.Event()

    async def gate(part):
        if part == fail:
            raise ConnectionError("controlled publication failure")
        if part == stall:
            entered.set()
            await asyncio.Event().wait()

    async def journal(messages):
        await gate("journal")
        batches.append(list(messages))
        if isinstance(runtime.repository, Repository):
            return await runtime.repository.save_bus_messages(messages)
        result = {m.event_id: JournalWriteResult(inserted=m.event_id not in journaled,
            broker_published=m.event_id in confirmed) for m in messages}
        journaled.update({m.event_id: m for m in messages})
        return result

    class Transport:
        async def publish_many(self, messages):
            await gate("broker")
            return ["broker-" + m.event_id for m in messages]

    async def confirm(published):
        await gate("confirmation")
        if isinstance(runtime.repository, Repository):
            await runtime.repository.mark_bus_messages_published(published)
        confirmed.update(published)

    bus = EventBus(journal_many=journal, transport=Transport(), transport_required=True,
                   mark_published_many=confirm)
    return bus, batches, confirmed, entered


async def test_ordered_admission_preserves_every_event_and_ack_before_analysis(lanes, monkeypatch):
    runtime, requests = lanes
    bus, batches, confirmed, _ = publication_bus(runtime)
    runtime._event_bus = bus
    delivered = []

    async def delivered_event(message):
        delivered.append(message)
    bus.subscribe(Topics.AGENT_REQUESTS, delivered_event)
    bus.subscribe(Topics.AGENT_OUTPUTS, delivered_event)
    run = runtime._run_agent

    async def checked_run(job, agent):
        assert len(batches) == 1 and len(batches[0]) == len(confirmed) == 6
        assert [m.event_type for m in batches[0]] == [EventTypes.AGENT_REQUESTED]*3 + [EventTypes.AGENT_STARTED]*3
        assert [m.event_type for m in delivered] == [m.event_type for m in batches[0]]
        return await run(job, agent)
    monkeypatch.setattr(runtime, "_run_agent", checked_run)
    outputs = await runtime.execute_primary(requests[:3])
    assert len(outputs) == 3 and len(batches) == 2
    assert len(batches[1]) == 3 and all(m.event_type == EventTypes.AGENT_COMPLETED for m in batches[1])
    requested, started = batches[0][:3], batches[0][3:]
    assert all(m.payload["status"] == "PENDING" and m.payload["attempt_count"] == 0 for m in requested)
    assert all(m.payload["status"] == "LEASED" and m.payload["attempt_count"] == 1 for m in started)
    profile = safe_primary_profile(runtime.last_primary_profile)
    parts = profile["stages"]["admission_events"]["publication"]
    assert all(parts[p]["status"] == "COMPLETED" for p in ("lock_wait", "journal", "broker", "confirmation", "delivery"))
    assert "requested_events" not in profile["stages"] and "started_events" not in profile["stages"]
    if isinstance(runtime.repository, Repository):
        assert parts["journal"]["db_commit_calls"] == parts["confirmation"]["db_commit_calls"] == 1
        async with runtime.repository._db.session() as session:
            assert await session.scalar(select(func.count()).select_from(EventJournalModel)) == 9
            assert await session.scalar(select(func.count()).select_from(EventOutboxModel).where(EventOutboxModel.published_at.is_not(None))) == 9
    # A repeated cohort keeps every durable id/attempt, and doesn't analyze again.
    monkeypatch.setattr(runtime, "_run_agent", lambda *args: pytest.fail("duplicate analysis"))
    assert [o.agent_name for o in await runtime.execute_primary(requests[:3])] == [o.agent_name for o in outputs]
    for job in await runtime.list_jobs():
        trace = await runtime.trace(job.execution_id)
        assert len(trace.attempts) == 1 and len(trace.memory) == 3


@pytest.mark.parametrize("part", ["journal", "broker", "confirmation"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_admission_failure_or_stall_cannot_reach_analysis(lanes, monkeypatch, part, cancel):
    runtime, requests = lanes
    bus, _, _, entered = publication_bus(runtime, fail=None if cancel else part, stall=part if cancel else None)
    runtime._event_bus = bus
    async def forbidden(*args):
        pytest.fail("Analysis must not precede every required publication acknowledgement")
    monkeypatch.setattr(runtime, "_run_agent", forbidden)
    task = asyncio.create_task(runtime.execute_primary(requests[:3]))
    if cancel:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else ConnectionError):
        await task
    profile = safe_primary_profile(runtime.last_primary_profile)
    row = profile["stages"]["admission_events"]
    assert row["status"] == row["publication"][part]["status"] == ("CANCELLED" if cancel else "FAILED")
    assert "analysis" not in profile["stages"]
    assert "controlled publication failure" not in json.dumps(profile)
    for job in await runtime.list_jobs():
        trace = await runtime.trace(job.execution_id)
        assert trace.job.status == "LEASED" and not trace.attempts
        assert [m.entry_type for m in trace.memory] == ["INPUT"]


async def test_confirmed_events_survive_bus_restart_without_duplicate_analysis(lanes):
    runtime, requests = lanes
    bus, _, confirmed, _ = publication_bus(runtime)
    runtime._event_bus = bus
    await runtime.execute_primary(requests[:3])
    first_ids = set(confirmed)
    runtime._event_bus = publication_bus(runtime)[0]
    assert len(await runtime.execute_primary(requests[:3])) == 3
    assert len(first_ids) == 9
    for job in await runtime.list_jobs():
        trace = await runtime.trace(job.execution_id)
        assert len(trace.attempts) == 1 and len(trace.memory) == 3


async def test_lost_admission_confirmation_recovers_lease_after_runtime_restart(lanes):
    runtime, requests = lanes
    runtime._lease_seconds = 1
    runtime._event_bus = publication_bus(runtime, fail="confirmation")[0]
    with pytest.raises(ConnectionError):
        await runtime.execute_primary(requests[:3])
    before = {job.execution_id: (await runtime.trace(job.execution_id)).memory[0].payload_hash
              for job in await runtime.list_jobs()}
    restarted = AgentRuntime(runtime.registry, repository=runtime.repository,
                             event_bus=publication_bus(runtime)[0])
    restarted.primary_runner, restarted.shadow_runner = runtime.primary_runner, runtime.shadow_runner
    await asyncio.sleep(1.05)
    assert len(await restarted.execute_primary(requests[:3])) == 3
    for job in await restarted.list_jobs():
        trace = await restarted.trace(job.execution_id)
        # Reclaiming an unfinished lease resumes the same durable attempt.
        assert len(trace.attempts) == 1 and trace.attempts[0].attempt_number == 1
        assert len(trace.memory) == 3 and trace.memory[0].payload_hash == before[job.execution_id]
    if isinstance(runtime.repository, Repository):
        async with runtime.repository._db.session() as session:
            # Requested/started IDs remain deterministic across recovery.
            assert await session.scalar(select(func.count()).select_from(EventJournalModel)) == 9


async def test_concurrent_admission_keeps_logical_requested_state_and_one_attempt(lanes):
    runtime, requests = lanes
    bus, batches, _, _ = publication_bus(runtime)
    runtime._event_bus = bus
    result = await asyncio.wait_for(asyncio.gather(
        runtime.execute_primary(requests[:3]), runtime.execute_primary(requests[:3])), 5)
    assert [len(outputs) for outputs in result] == [3, 3]
    requested = [m for batch in batches for m in batch if m.event_type == EventTypes.AGENT_REQUESTED]
    assert requested and all(m.payload["status"] == "PENDING" and m.payload["attempt_count"] == 0 for m in requested)
    if isinstance(runtime.repository, Repository):
        async with runtime.repository._db.session() as session:
            rows = list(await session.scalars(select(EventJournalModel)))
            assert len(rows) == 9
            assert len({m.event_id for m in rows}) == 9
    for job in await runtime.list_jobs():
        assert len((await runtime.trace(job.execution_id)).attempts) == 1

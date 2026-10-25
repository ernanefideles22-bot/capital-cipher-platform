"""Reproduce the hosted PRIMARY/analytical-worker race on both repositories."""
import asyncio
import os

import pytest

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime, AgentRuntimeWorker
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import AgentStatus, Signal


class LaneAgent(BaseAgent):
    async def _analyze(self, agent_input):
        await asyncio.sleep(.01)
        return self._output(AgentStatus.COMPLETED, Signal.HOLD, 50, "Lane regression fixture")


class InlineRunner:
    async def run(self, agent, agent_input):
        return await agent.run(agent_input)


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres",
    marks=pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured"))])
async def lanes(request, tmp_path):
    database = None
    repository = None
    if request.param == "sqlite":
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lanes.db'}")
        await database.create_all()
        repository = Repository(database)
    elif request.param == "postgres":
        import asyncpg
        from pathlib import Path
        from urllib.parse import urlsplit, urlunsplit
        from uuid import uuid4
        url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
        assert url.hostname in {"localhost", "127.0.0.1", "::1"}, "CI fixture refuses hosted databases"
        name = "capital_cipher_paper_validation_" + uuid4().hex[:12] + "_flow"
        control = await asyncpg.connect(urlunsplit(url))
        try:
            assert int(await control.fetchval("show server_version_num")) // 10000 == 17
            await control.execute(f'create database "{name}" template template0')
        finally:
            await control.close()
        target = urlunsplit(url._replace(path="/" + name))
        connection = await asyncpg.connect(target)
        try:
            for migration in sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql")):
                await connection.execute(migration.read_text())
        finally:
            await connection.close()
        database = Database(target.replace("postgresql://", "postgresql+asyncpg://"), pool_size=4)
        repository = Repository(database)
    agents = []
    for name, role in [("PrimaryA", "PRIMARY"), ("PrimaryB", "PRIMARY"),
                       ("PrimaryC", "PRIMARY"), ("Research", "SHADOW")]:
        agent = LaneAgent()
        agent.name, agent.decision_role = name, role
        agents.append(agent)
    runtime = AgentRuntime(AgentRegistry(agents), repository=repository)
    runtime.primary_runner = runtime.shadow_runner = InlineRunner()
    await runtime.initialize()
    requests = [AgentExecutionRequest(agent_version=a.version,
        input=AgentInput(agent_name=a.name, request_id=f"request-{a.name}",
            correlation_id="lane-regression", symbol="BTCUSDT", timeframe="15m")) for a in agents]
    try:
        yield runtime, requests
    finally:
        if database:
            await database.dispose()


async def test_analytical_worker_cannot_take_any_pending_primary(lanes):
    runtime, requests = lanes
    jobs = await runtime.submit_many(requests)
    worker = AgentRuntimeWorker(runtime, worker_id="analytical", claim_batch_size=4)
    processed = await worker.run_once()
    assert processed.decision_role == "SHADOW"
    for job in jobs[:3]:
        assert (await runtime.trace(job.execution_id)).job.status == "PENDING"
    assert await worker.run_once() is None
    outputs = await runtime.execute_primary(requests[:3])
    assert len(outputs) == 3 and all(o.status == AgentStatus.COMPLETED for o in outputs)
    for job in jobs[:3]:
        trace = await runtime.trace(job.execution_id)
        assert len(trace.attempts) == 1
        assert trace.attempts[0].worker_id.startswith("office-primary-")
        assert len(trace.memory) == 3


async def test_concurrent_primary_cohort_is_exactly_once_and_preserves_order(lanes):
    runtime, requests = lanes
    requests = list(reversed(requests[:3]))
    results = await asyncio.wait_for(asyncio.gather(
        runtime.execute_primary(requests), runtime.execute_primary(requests)), 5)
    assert [[o.agent_name for o in result] for result in results] == [[r.input.agent_name for r in requests]] * 2
    jobs = await runtime.list_jobs()
    assert len(jobs) == 3
    for job in jobs:
        trace = await runtime.trace(job.execution_id)
        assert trace.job.status == "COMPLETED"
        assert len(trace.attempts) == 1 and len(trace.memory) == 3


async def test_mixed_batch_keeps_generic_api_compatible_with_shadow_only_workers(lanes):
    runtime, requests = lanes
    stop = asyncio.Event()
    worker = AgentRuntimeWorker(runtime, worker_id="analytical", poll_interval_seconds=.01)
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0)
    ordered = [requests[3], requests[1], requests[0], requests[2]]
    try:
        outputs = await asyncio.wait_for(runtime.execute_many(ordered), 5)
    finally:
        stop.set()
        await task
    assert [o.agent_name for o in outputs] == [r.input.agent_name for r in ordered]
    assert all(o.status == AgentStatus.COMPLETED for o in outputs)


@pytest.mark.parametrize("scope", ["empty", "wrong_role", "exact"])
async def test_scoped_claim_never_drains_unrelated_jobs(lanes, scope):
    runtime, requests = lanes
    jobs = await runtime.submit_many(requests)
    ids = [] if scope == "empty" else [jobs[0].execution_id]
    claimed = await runtime.repository.claim_next_agent_executions(
        worker_id="scoped", lease_seconds=30, limit=4,
        execution_ids=ids, decision_role="SHADOW" if scope == "wrong_role" else "PRIMARY")
    assert [j.execution_id for j in claimed] == ([jobs[0].execution_id] if scope == "exact" else [])
    for job in jobs[1:]:
        assert (await runtime.trace(job.execution_id)).job.status == "PENDING"


async def test_expired_primary_lease_recovers_without_analytical_worker(lanes):
    runtime, requests = lanes
    jobs = await runtime.submit_many(requests[:3])
    claimed = await runtime.repository.claim_next_agent_executions(
        worker_id="stopped-primary", lease_seconds=1, limit=3, decision_role="PRIMARY")
    assert len(claimed) == 3
    await asyncio.sleep(1.05)
    assert len(await runtime.execute_primary(requests[:3])) == 3
    for job in jobs:
        trace = await runtime.trace(job.execution_id)
        assert trace.job.status == "COMPLETED" and len(trace.attempts) == 1
        assert trace.attempts[0].worker_id.startswith("office-primary-")


async def test_fresh_primary_commits_input_with_lease_without_a_second_claim(lanes, monkeypatch):
    runtime, requests = lanes
    async def forbidden(**kwargs):
        raise AssertionError("New PRIMARY must not incur another claim transaction")
    monkeypatch.setattr(runtime.repository, "claim_next_agent_executions", forbidden)
    outputs = await runtime.execute_primary(requests[:3])
    assert len(outputs) == 3
    profile = runtime.last_primary_profile
    assert "claim_jobs" not in profile["stages"]
    if isinstance(runtime.repository, Repository):
        # Performance budget on actual DB statements, without generated-id reads.
        assert profile["stages"]["create_jobs"]["sql_statements"] <= 2
        assert profile["stages"]["finish_jobs"]["sql_statements"] <= 6
    for job in await runtime.list_jobs():
        trace = await runtime.trace(job.execution_id)
        assert job.status == "COMPLETED" and len(trace.attempts) == 1 and len(trace.memory) == 3


async def test_reservation_does_not_overwrite_another_primary_owner(lanes):
    runtime, requests = lanes
    prepared = [runtime._prepare_submission(r) for r in requests[:3]]
    first = await runtime.repository.create_agent_executions(prepared,
        claim_new_primary_by="office-primary-existing", lease_seconds=30)
    again = await runtime.repository.create_agent_executions(prepared,
        claim_new_primary_by="office-primary-second", lease_seconds=30)
    assert all(job.leased_by == "office-primary-existing" and job.attempt_count == 1 for job in again)
    await runtime.execute_claimed_many(first, worker_id="office-primary-existing")
    assert len(await runtime.execute_primary(requests[:3])) == 3
    for job in await runtime.list_jobs():
        assert len((await runtime.trace(job.execution_id)).attempts) == 1


async def test_duplicate_requests_in_new_reserved_batch_remain_one_attempt(lanes):
    runtime, requests = lanes
    outputs = await runtime.execute_primary([requests[0], requests[0]])
    assert len(outputs) == 2 and outputs[0] == outputs[1]
    job, = await runtime.list_jobs()
    trace = await runtime.trace(job.execution_id)
    assert len(trace.attempts) == 1 and len(trace.memory) == 3


@pytest.mark.parametrize("phase", ["input", "output"])
async def test_sql_failure_rolls_back_reserved_input_and_terminal_evidence(lanes, phase):
    from sqlalchemy import event
    from app.core.errors import DatabaseError
    runtime, requests = lanes
    if not isinstance(runtime.repository, Repository):
        pytest.skip("SQL rollback case")
    engine = runtime.repository._db.engine.sync_engine
    table = "agent_memory_entries" if phase == "input" else "agent_outputs"
    def fail(conn, cursor, statement, parameters, execution_context, executemany):
        compiled = execution_context.compiled
        if compiled is not None and compiled.isinsert and compiled.statement.table.name == table:
            raise RuntimeError("Controlled SQL failure")
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(DatabaseError):
            await runtime.execute_primary(requests[:3])
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    jobs = await runtime.list_jobs()
    if phase == "input":
        assert not jobs
    else:
        assert len(jobs) == 3
        for job in jobs:
            trace = await runtime.trace(job.execution_id)
            assert job.status == "LEASED" and job.attempt_count == 1
            assert not trace.attempts and len(trace.memory) == 1
        await runtime.execute_claimed_many(jobs, worker_id=jobs[0].leased_by)
        assert all([len((await runtime.trace(job.execution_id)).attempts) == 1 for job in jobs])

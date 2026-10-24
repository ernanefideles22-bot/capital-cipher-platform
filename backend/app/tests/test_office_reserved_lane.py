"""Reproduce the hosted PRIMARY/analytical-worker race on both repositories."""
import asyncio

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


@pytest.fixture(params=["memory", "sqlite"])
async def lanes(request, tmp_path):
    database = None
    repository = None
    if request.param == "sqlite":
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lanes.db'}")
        await database.create_all()
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

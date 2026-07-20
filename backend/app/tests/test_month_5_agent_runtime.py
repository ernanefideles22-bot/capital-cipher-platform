"""Month 5 runtime invariants retained by the expanded Month 8 cohort."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import (
    AgentRuntime,
    AgentRuntimeWorker,
    InMemoryAgentRuntimeRepository,
)
from app.api.context import build_context
from app.core.config import Settings
from app.core.errors import ConfigurationError, ValidationError
from app.core.event_bus import EventBus, Topics
from app.database.models import (
    AgentExecutionAttemptModel,
    AgentMemoryEntryModel,
    AgentOutputModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.orchestrator.decision_engine import DecisionEngine
from app.schemas.agents import (
    AgentExecutionRequest,
    AgentInput,
    AgentOutput,
)
from app.schemas.common import AgentStatus, Signal, utcnow
from app.schemas.events import EventTypes
from app.tests.conftest import make_series


class SuccessfulAgent(BaseAgent):
    name = "SuccessfulAgent"
    description = "Deterministic runtime test agent"
    capabilities = ("runtime-test",)
    max_attempts = 3

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        return self._output(
            AgentStatus.COMPLETED,
            Signal.NEUTRAL,
            100,
            "Runtime test completed",
            evidence={"request_id": agent_input.request_id},
        )


class FlakyAgent(SuccessfulAgent):
    name = "FlakyAgent"

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures_remaining = failures

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("secret-value-must-never-enter-output")
        return await super()._analyze(agent_input)


class SlowAgent(SuccessfulAgent):
    name = "SlowAgent"
    timeout_ms = 1
    max_attempts = 2

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        await asyncio.sleep(0.05)
        return await super()._analyze(agent_input)


class SensitiveOutputAgent(SuccessfulAgent):
    name = "SensitiveOutputAgent"

    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        return self._output(
            AgentStatus.COMPLETED,
            Signal.NEUTRAL,
            100,
            "This output must be rejected before persistence",
            evidence={"api_key": "must-not-be-persisted"},
        )


def _request(
    agent: BaseAgent,
    *,
    request_id: str = "runtime-request-1",
    idempotency_key: str = "runtime-idempotency-1",
) -> AgentExecutionRequest:
    return AgentExecutionRequest(
        agent_version=agent.version,
        idempotency_key=idempotency_key,
        input=AgentInput(
            request_id=request_id,
            correlation_id="runtime-correlation-1",
            agent_name=agent.name,
            symbol="BTCUSDT",
            timeframe="15m",
        ),
    )


async def test_default_registry_contains_exactly_300_paper_agents():
    context = build_context(Settings(), with_database=False)
    assert context.agent_registry is not None
    assert context.agent_runtime is not None
    await context.agent_runtime.initialize()

    registrations = context.agent_registry.registrations()
    names = {registration.agent_name for registration in registrations}

    assert len(registrations) == 300
    assert sum(
        registration.decision_role == "PRIMARY"
        for registration in registrations
    ) == 3
    assert sum(
        registration.decision_role == "SHADOW"
        for registration in registrations
    ) == 297
    assert {
        "MarketDataAgent",
        "QuantAgent",
        "TrendAgent",
        "MomentumAgent",
        "VolatilityAgent",
        "VolumeAgent",
        "VWAPAgent",
        "MACDAgent",
        "EMAAlignmentAgent",
        "MeanReversionAgent",
        "BreakoutAgent",
        "SupportResistanceAgent",
        "CandleStructureAgent",
        "LiquidityProxyAgent",
        "DataQualityAgent",
    }.issubset(names)
    assert all(
        registration.execution_mode == "PAPER"
        and registration.capabilities
        and registration.definition_hash
        for registration in registrations
    )
    assert "PaperTradingAgent" not in names


async def test_all_300_agents_execute_through_versioned_runtime_contracts():
    context = build_context(Settings(), with_database=False)
    assert context.agent_runtime is not None
    assert context.agent_registry is not None
    for candle in make_series(
        [100 * (1.001**index) for index in range(80)]
    ):
        context.candle_store.add(candle)
    await context.agent_runtime.initialize()
    requests = [
        AgentExecutionRequest(
            agent_version=agent.version,
            idempotency_key=f"cohort-{agent.name}",
            input=AgentInput(
                request_id=f"cohort-{agent.name}",
                correlation_id="cohort-correlation",
                agent_name=agent.name,
                symbol="BTCUSDT",
                timeframe="15m",
                market_context={"exchange": "BINANCE"},
            ),
        )
        for agent in context.agent_registry.agents.values()
    ]

    outputs = await context.agent_runtime.execute_many(requests)

    assert len(outputs) == 300
    assert {output.agent_name for output in outputs} == {
        registration.agent_name
        for registration in context.agent_registry.registrations()
    }
    assert all(
        output.status in {
            AgentStatus.COMPLETED,
            AgentStatus.BLOCKED,
            AgentStatus.SKIPPED,
        }
        for output in outputs
    )


def test_registry_rejects_implicit_replacement_and_keeps_history():
    first = SuccessfulAgent()
    registry = AgentRegistry([first])
    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register(SuccessfulAgent())

    class SuccessfulAgentV2(SuccessfulAgent):
        version = "2.0.0"

    replacement = SuccessfulAgentV2()
    registered = registry.replace(
        replacement,
        expected_version="1.0.0",
    )
    removed = registry.remove(replacement.name)

    assert registered.version == "2.0.0"
    assert removed.enabled is False
    assert len(registry.history) == 2
    with pytest.raises(ValidationError, match="not registered"):
        registry.get(replacement.name)


def test_shadow_agent_output_cannot_change_operational_decision():
    primary = [
        AgentOutput(
            agent_name="MarketDataAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.NEUTRAL,
            confidence=100,
            reason="Primary market data is valid",
        ),
        AgentOutput(
            agent_name="QuantAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.BUY,
            confidence=95,
            reason="Primary quant signal",
        ),
        AgentOutput(
            agent_name="TrendAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.BUY,
            confidence=95,
            reason="Primary trend signal",
        ),
    ]
    shadow = AgentOutput(
        agent_name="MomentumAgent",
        status=AgentStatus.TIMEOUT,
        signal=Signal.SELL,
        confidence=100,
        reason="Shadow output must remain evidence-only",
        warnings=["SHADOW_WARNING_MUST_NOT_PROPAGATE"],
    )
    engine = DecisionEngine()

    baseline = engine.consolidate(
        correlation_id="shadow-isolation",
        symbol="BTCUSDT",
        timeframe="15m",
        agent_outputs=primary,
    )
    observed = engine.consolidate(
        correlation_id="shadow-isolation",
        symbol="BTCUSDT",
        timeframe="15m",
        agent_outputs=[*primary, shadow],
    )

    assert observed.candidate_action == baseline.candidate_action
    assert observed.confidence == baseline.confidence
    assert observed.warnings == baseline.warnings
    assert len(observed.agent_summary) == len(baseline.agent_summary) + 1


async def test_runtime_retries_are_bounded_and_memory_is_execution_scoped():
    agent = FlakyAgent(failures=2)
    runtime = AgentRuntime(
        AgentRegistry([agent]),
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    trace = await runtime.execute(_request(agent))

    assert trace.job.status == "COMPLETED"
    assert trace.job.attempt_count == 3
    assert [attempt.status for attempt in trace.attempts] == [
        AgentStatus.FAILED,
        AgentStatus.FAILED,
        AgentStatus.COMPLETED,
    ]
    assert [entry.sequence for entry in trace.memory] == [1, 2, 4, 6, 7]
    assert [entry.entry_type for entry in trace.memory] == [
        "INPUT",
        "ATTEMPT",
        "ATTEMPT",
        "ATTEMPT",
        "OUTPUT",
    ]
    serialized = trace.model_dump_json()
    assert "secret-value-must-never-enter-output" not in serialized
    assert all(
        entry.execution_id == trace.job.execution_id
        for entry in trace.memory
    )


async def test_timeout_exhaustion_is_dead_lettered_fail_safe():
    agent = SlowAgent()
    runtime = AgentRuntime(
        AgentRegistry([agent]),
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    trace = await runtime.execute(_request(agent))

    assert trace.job.status == "DEAD_LETTER"
    assert trace.job.attempt_count == 2
    assert trace.job.output is not None
    assert trace.job.output.status == AgentStatus.TIMEOUT
    assert trace.memory[-1].entry_type == "DEAD_LETTER"


async def test_submission_is_idempotent_and_conflicts_fail_closed():
    agent = SuccessfulAgent()
    runtime = AgentRuntime(AgentRegistry([agent]))
    request = _request(agent)

    first = await runtime.execute(request)
    repeated = await runtime.execute(request)

    assert repeated == first
    conflicting = _request(
        agent,
        request_id="different-input",
        idempotency_key=request.idempotency_key or "",
    )
    with pytest.raises(ValidationError, match="conflicts"):
        await runtime.submit(conflicting)


async def test_concurrent_idempotent_execution_records_one_attempt():
    agent = SuccessfulAgent()
    runtime = AgentRuntime(AgentRegistry([agent]))
    request = _request(agent)

    first, second = await asyncio.gather(
        runtime.execute(request),
        runtime.execute(request),
    )

    assert first == second
    assert first.job.status == "COMPLETED"
    assert len(first.attempts) == 1
    assert agent.total_runs == 1


async def test_sensitive_agent_output_is_sanitized_and_dead_lettered():
    agent = SensitiveOutputAgent()
    runtime = AgentRuntime(AgentRegistry([agent]))

    trace = await runtime.execute(_request(agent))
    serialized = trace.model_dump_json()

    assert trace.job.status == "DEAD_LETTER"
    assert trace.job.output is not None
    assert trace.job.output.warnings == ["AGENT_OUTPUT_REJECTED"]
    assert "must-not-be-persisted" not in serialized
    assert "api_key" not in serialized


async def test_agent_runtime_publishes_correlated_lifecycle_events():
    agent = FlakyAgent(failures=1)
    bus = EventBus()
    received = []

    async def capture(message):
        received.append(message)

    bus.subscribe(Topics.AGENT_REQUESTS, capture)
    bus.subscribe(Topics.AGENT_OUTPUTS, capture)
    runtime = AgentRuntime(
        AgentRegistry([agent]),
        event_bus=bus,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    trace = await runtime.execute(_request(agent))

    assert trace.job.status == "COMPLETED"
    assert [message.event_type for message in received] == [
        EventTypes.AGENT_REQUESTED,
        EventTypes.AGENT_STARTED,
        EventTypes.AGENT_RETRY_SCHEDULED,
        EventTypes.AGENT_STARTED,
        EventTypes.AGENT_COMPLETED,
    ]
    assert {
        message.correlation_id for message in received
    } == {"runtime-correlation-1"}
    assert received[-1].payload["output"]["agent_name"] == agent.name
    assert received[-1].payload["output"]["status"] == "COMPLETED"


async def test_changed_agent_definition_is_dead_lettered_without_running():
    agent = SuccessfulAgent()
    runtime = AgentRuntime(AgentRegistry([agent]))
    job = await runtime.enqueue(_request(agent))
    agent.description = "Definition changed without a version bump"
    worker = AgentRuntimeWorker(runtime, worker_id="definition-worker")

    processed = await worker.run_once()
    trace = await runtime.trace(job.execution_id)

    assert processed is not None
    assert processed.status == "DEAD_LETTER"
    assert trace is not None
    assert trace.job.output is not None
    assert trace.job.output.warnings == ["AGENT_DEFINITION_MISMATCH"]
    assert agent.total_runs == 0


async def test_queue_worker_processes_enqueued_job():
    agent = SuccessfulAgent()
    runtime = AgentRuntime(AgentRegistry([agent]))
    worker = AgentRuntimeWorker(
        runtime,
        worker_id="test-worker",
        poll_interval_seconds=0.01,
    )
    submitted = await runtime.enqueue(_request(agent))

    processed = await worker.run_once()
    trace = await runtime.trace(submitted.execution_id)

    assert processed is not None
    assert processed.status == "COMPLETED"
    assert trace is not None
    assert trace.job.output is not None


async def test_expired_lease_is_recovered_without_consuming_new_attempt():
    agent = SuccessfulAgent()
    store = InMemoryAgentRuntimeRepository()
    runtime = AgentRuntime(AgentRegistry([agent]), repository=store)
    request = _request(agent)
    job = await runtime.enqueue(request)
    first_claim = await store.claim_agent_execution(
        job.execution_id,
        worker_id="crashed-worker",
        lease_seconds=30,
    )
    assert first_claim is not None
    store._jobs[job.execution_id] = first_claim.model_copy(
        update={"lease_expires_at": utcnow() - timedelta(seconds=1)}
    )

    recovered_trace = await asyncio.wait_for(
        runtime.execute(request),
        timeout=1,
    )

    assert recovered_trace.job.status == "COMPLETED"
    assert recovered_trace.job.attempt_count == 1
    assert len(recovered_trace.attempts) == 1


async def test_sqlite_runtime_persists_trace_and_immutable_evidence():
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_all()
    repository = Repository(database)
    agent = FlakyAgent(failures=1)
    runtime = AgentRuntime(
        AgentRegistry([agent]),
        repository=repository,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )

    trace = await runtime.execute(_request(agent))
    loaded = await repository.load_agent_execution_trace(
        trace.job.execution_id
    )
    async with database.session() as session:
        output_count = await session.scalar(
            select(func.count()).select_from(AgentOutputModel)
        )

    assert loaded == trace
    assert output_count == 1
    conflicting = _request(
        agent,
        request_id="sqlite-different-input",
        idempotency_key="runtime-idempotency-1",
    )
    with pytest.raises(ValidationError, match="conflicts"):
        await runtime.submit(conflicting)
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(AgentExecutionAttemptModel).values(
                    worker_id="tampered"
                )
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(delete(AgentMemoryEntryModel))
    await database.dispose()


async def test_agent_execution_api_is_authenticated_and_traceable():
    admin_key = "m" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        AGENT_WORKER_ENABLED=False,
        BACKFILL_WORKER_ENABLED=False,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    body = AgentExecutionRequest(
        agent_version="1.0.0",
        idempotency_key="api-agent-execution",
        input=AgentInput(
            request_id="api-agent-execution",
            correlation_id="api-agent-correlation",
            agent_name="MomentumAgent",
            symbol="BTCUSDT",
            timeframe="15m",
        ),
    ).model_dump(mode="json")

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.post(
                "/api/v1/agents/executions",
                json=body,
            )
            created = await client.post(
                "/api/v1/agents/executions",
                headers={"X-API-Key": admin_key},
                json=body,
            )
            execution_id = created.json()["data"]["execution"][
                "execution_id"
            ]
            assert context.agent_runtime_worker is not None
            await context.agent_runtime_worker.run_once()
            loaded = await client.get(
                f"/api/v1/agents/executions/{execution_id}",
                headers={"X-API-Key": admin_key},
            )
            listed = await client.get(
                "/api/v1/agents/executions",
                headers={"X-API-Key": admin_key},
            )

    assert denied.status_code == 401
    assert created.status_code == 200
    assert loaded.json()["data"]["trace"]["job"]["status"] == "COMPLETED"
    assert len(loaded.json()["data"]["trace"]["memory"]) == 3
    assert len(listed.json()["data"]["executions"]) == 1


def test_agent_runtime_migration_is_private_bounded_and_append_only():
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
        / "20260720074545_create_agent_runtime.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(migration.lower().split())

    assert "create schema if not exists capital_cipher" in normalized
    assert normalized.count("create table if not exists capital_cipher.agent_") == 3
    assert "check (execution_mode = 'paper')" in normalized
    assert "attempt_count <= max_attempts" in normalized
    assert "max_attempts <= 10" in normalized
    assert "where status in ('pending', 'retry')" in normalized
    assert "where status = 'leased'" in normalized
    assert normalized.count("enable row level security") == 3
    assert "security invoker" in normalized
    assert normalized.count("before update or delete") == 2
    assert "revoke all on schema capital_cipher from public" in normalized
    assert "revoke all on table" in normalized
    assert "revoke all on sequence" in normalized

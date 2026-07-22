"""Month 10 invariants retained by the expanded 300-agent cohort."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError as SQLAlchemyDatabaseError

from app.agents.month10_specialists import MONTH10_RESILIENCE_DEFINITIONS
from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.operations.load_testing import AgentLoadHarness, DeterministicChaosHarness
from app.operations.metrics import BoundedMetricRegistry
from app.operations.resilience import RecoveryCoordinator
from app.operations.service import OperationsService
from app.schemas.agents import AgentInput
from app.schemas.operations import (
    CostUsageRecord,
    OperationalAlertEvent,
    OperationalMetricPoint,
    OperationalMetricSnapshot,
    ResilienceTestRun,
    SLOEvaluation,
)
from app.tests.conftest import make_series

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "packages" / "contracts" / "schemas" / "v1"
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260720201528_create_operational_observability_resilience.sql"
)


def _context(*, max_concurrency: int = 32):
    return build_context(
        Settings(
            AGENT_MAX_CONCURRENCY=max_concurrency,
            AGENT_WORKER_ENABLED=False,
            BACKFILL_WORKER_ENABLED=False,
            OMS_WORKER_ENABLED=False,
            OMS_RECONCILIATION_ENABLED=False,
            OPERATIONS_MONITOR_ENABLED=False,
        ),
        with_database=False,
    )


def _artifacts():
    snapshot = OperationalMetricSnapshot(
        correlation_id="month10-persistence",
        window_seconds=300,
        registered_agents=200,
        active_agents=200,
        metrics=[
            OperationalMetricPoint(
                name="agents.executions",
                kind="COUNTER",
                value=200,
                sample_count=200,
            )
        ],
    )
    slo = SLOEvaluation(
        slo_name="agents.execution_success_rate",
        comparator="GTE",
        target=0.99,
        measured=1.0,
        sample_count=200,
        compliant=True,
        error_budget_remaining_percent=100,
        status="HEALTHY",
        window_seconds=300,
    )
    alert = OperationalAlertEvent(
        alert_key="dependency:broker",
        lifecycle_sequence=1,
        event_type="OPENED",
        severity="WARNING",
        source="month10-test",
        correlation_id="month10-persistence",
        reason="deterministic evidence",
    )
    cost = CostUsageRecord(
        cost_center="AGENT_RUNTIME",
        resource="paper-agent-execution",
        quantity=200,
        unit="execution",
        unit_cost_usd=0.001,
        estimated_cost_usd=0.2,
        correlation_id="month10-persistence",
    )
    run = DeterministicChaosHarness.optional_broker_outage()
    return snapshot, slo, alert, cost, run


def test_month10_catalog_adds_50_unique_bounded_specialists():
    assert len(MONTH10_RESILIENCE_DEFINITIONS) == 50
    assert len({item.name for item in MONTH10_RESILIENCE_DEFINITIONS}) == 50
    assert {
        item.family for item in MONTH10_RESILIENCE_DEFINITIONS
    } == {
        "ENTROPY",
        "JUMP_INTENSITY",
        "VOLUME_ANOMALY",
        "GAP_PRESSURE",
        "TREND_PERSISTENCE",
        "VOL_OF_VOL",
        "CLOSE_LOCATION",
        "WICK_IMBALANCE",
        "DOWNSIDE_CAPTURE",
    }


def test_runtime_has_exactly_300_paper_agents_with_bounded_authority():
    context = _context()
    assert context.agent_registry is not None
    registrations = context.agent_registry.registrations()
    month10_names = {item.name for item in MONTH10_RESILIENCE_DEFINITIONS}

    assert len(registrations) == 300
    assert sum(item.decision_role == "PRIMARY" for item in registrations) == 3
    assert sum(item.decision_role == "SHADOW" for item in registrations) == 297
    assert month10_names.issubset(
        {item.agent_name for item in registrations}
    )
    assert all(item.execution_mode == "PAPER" for item in registrations)
    assert all(
        not {
            "submit-order",
            "cancel-order",
            "exchange-credentials",
            "risk-approval",
        }.intersection(item.capabilities)
        for item in registrations
    )


async def test_month10_specialists_are_deterministic_and_read_only():
    context = _context()
    assert context.agent_registry is not None
    for candle in make_series(
        [100 * (1.0005**index) for index in range(240)],
        volume=125,
    ):
        context.candle_store.add(candle)
    agent = context.agent_registry.get(MONTH10_RESILIENCE_DEFINITIONS[0].name)
    agent_input = AgentInput(
        request_id="month10-determinism",
        correlation_id="month10-determinism",
        agent_name=agent.name,
        symbol="BTCUSDT",
        timeframe="15m",
        market_context={"exchange": "BINANCE"},
    )

    first = await agent.run(agent_input)
    second = await agent.run(agent_input)

    assert (
        first.status,
        first.signal,
        first.confidence,
        first.reason,
        first.evidence,
    ) == (
        second.status,
        second.signal,
        second.confidence,
        second.reason,
        second.evidence,
    )
    assert first.evidence["read_only"] is True
    assert first.evidence["decision_authority"] is False
    assert first.evidence["order_authority"] is False
    assert not hasattr(agent, "_risk")
    assert not hasattr(agent, "_credentials")


def test_metric_registry_is_bounded_and_computes_p95():
    metrics = BoundedMetricRegistry(max_samples_per_metric=100)
    for value in range(150):
        metrics.observe("agents.execution_latency_ms", float(value))
    metrics.increment("agents.executions", 150)
    metrics.gauge("cost.daily_utilization_percent", 25)

    summary = metrics.summary("agents.execution_latency_ms")
    snapshot = metrics.snapshot(
        correlation_id="month10-metrics",
        window_seconds=300,
        registered_agents=200,
        active_agents=200,
    )

    assert summary.count == metrics.capacity == 100
    assert summary.maximum == 149
    assert summary.p95 == 144
    assert len(snapshot.metrics) == 5
    assert snapshot.registered_agents == 200


def test_metric_registry_enforces_the_configured_time_window():
    now = [1_000.0]
    metrics = BoundedMetricRegistry(
        max_samples_per_metric=100,
        clock=lambda: now[0],
    )
    metrics.increment("agents.executions", 10)
    metrics.observe("agents.execution_latency_ms", 250)
    now[0] += 301

    assert metrics.counter("agents.executions") == 10
    assert (
        metrics.counter(
            "agents.executions",
            window_seconds=300,
        )
        == 0
    )
    assert metrics.summary("agents.execution_latency_ms").count == 1
    assert (
        metrics.summary(
            "agents.execution_latency_ms",
            window_seconds=300,
        ).count
        == 0
    )


def test_recovery_is_fail_closed_degraded_and_conservative():
    recovery = RecoveryCoordinator(recovery_successes_required=3)
    recovery.observe("BROKER", healthy=False, reason="optional outage")
    assert recovery.mode == "DEGRADED"
    assert recovery.decisions_allowed is True
    assert recovery.shadow_allowed is False

    recovery.observe("BROKER", healthy=True, reason="broker recovered")
    recovery.observe("DATABASE", healthy=False, reason="critical outage")
    assert recovery.mode == "SAFE_HALT"
    assert recovery.decisions_allowed is False

    for dependency in ("DATABASE", "AUDIT", "RISK"):
        for _ in range(2):
            recovery.observe(
                dependency,
                healthy=True,
                reason="recovery confirmation",
            )
    assert recovery.mode == "SAFE_HALT"
    for dependency in ("DATABASE", "AUDIT", "RISK"):
        recovery.observe(
            dependency,
            healthy=True,
            reason="final recovery confirmation",
        )
    assert recovery.mode == "HEALTHY"


async def test_cost_hard_limit_suspends_shadow_but_never_primary():
    context = _context()
    assert context.agent_registry is not None
    service = OperationsService(
        context.agent_registry,
        metric_capacity=100,
        daily_budget_usd=1,
        budget_warning_percent=80,
    )
    await service.record_cost(
        CostUsageRecord(
            cost_center="EXTERNAL_DATA",
            resource="bounded-paper-test",
            quantity=1,
            unit="request",
            unit_cost_usd=1.1,
            estimated_cost_usd=1.1,
        )
    )

    status = service.budget_status()
    admitted = service.admitted_registrations(
        context.agent_registry.registrations()
    )

    assert status.status == "HARD_LIMIT"
    assert status.shadow_admission_allowed is False
    assert status.primary_admission_allowed is True
    assert len(admitted) == 3
    assert all(item.decision_role == "PRIMARY" for item in admitted)
    assert service.decisions_allowed is True


async def test_slo_and_dependency_alerts_are_append_only_lifecycles():
    context = _context()
    assert context.agent_registry is not None
    service = OperationsService(
        context.agent_registry,
        metric_capacity=100,
        agent_success_target=0.99,
    )
    service.metrics.increment("agents.executions", 100)
    service.metrics.increment("agents.failures", 2)
    service.metrics.observe("agents.execution_latency_ms", 100)

    first = await service.evaluate_slos(correlation_id="month10-slo-1")
    second = await service.evaluate_slos(correlation_id="month10-slo-2")
    breached = [
        item for item in first
        if item.slo_name == "agents.execution_success_rate"
    ][0]

    assert breached.status == "BREACHED"
    assert len(second) == 4
    assert len(
        [
            event for event in service.alert_events()
            if event.alert_key == "slo:agents.execution_success_rate"
            and event.event_type == "OPENED"
        ]
    ) == 1

    await service.observe_dependency(
        "BROKER",
        healthy=False,
        reason="deterministic optional outage",
        correlation_id="month10-dependency",
    )
    await service.observe_dependency(
        "BROKER",
        healthy=True,
        reason="deterministic recovery",
        correlation_id="month10-dependency",
    )
    lifecycle = [
        event for event in reversed(service.alert_events())
        if event.alert_key == "dependency:broker"
    ]
    assert [item.event_type for item in lifecycle] == ["OPENED", "RESOLVED"]
    assert [item.lifecycle_sequence for item in lifecycle] == [1, 2]
    assert all(
        item.correlation_id == "month10-dependency"
        for item in lifecycle
    )


async def test_operational_monitor_probes_and_materializes_one_cycle():
    context = _context()
    assert context.agent_registry is not None
    service = OperationsService(
        context.agent_registry,
        metric_capacity=100,
    )
    stop_event = asyncio.Event()

    async def probe():
        stop_event.set()
        return {
            "DATABASE": (True, "database reachable"),
            "AUDIT": (True, "audit available"),
            "RISK": (True, "risk available"),
            "BROKER": (True, "broker optional"),
            "MARKET_DATA": (True, "market data available"),
            "SHADOW_RUNTIME": (True, "300 PAPER agents"),
        }

    await service.run(
        stop_event,
        probe=probe,
        interval_seconds=1,
    )

    assert len(service.snapshots()) == 1
    assert service.snapshots()[0].registered_agents == 300
    assert len(service.slo_evaluations()) == 4
    assert service.status()["recovery"]["mode"] == "HEALTHY"


def test_deterministic_chaos_harness_proves_safe_degradation():
    runs = [
        DeterministicChaosHarness.critical_database_outage(),
        DeterministicChaosHarness.optional_broker_outage(),
    ]
    assert all(run.status == "PASSED" for run in runs)
    assert all(run.live_execution_attempted is False for run in runs)
    assert all(run.environment == "CI" for run in runs)
    assert runs[0].invariants["critical_failure_halts_decisions"] is True
    assert runs[1].invariants["shadow_work_is_suspended"] is True


async def test_load_harness_executes_all_300_agents_within_paper_slos():
    context = _context(max_concurrency=32)
    assert context.agent_runtime is not None
    for candle in make_series(
        [100 * (1.0005**index) for index in range(240)],
        volume=150,
    ):
        context.candle_store.add(candle)
    await context.agent_runtime.initialize()

    run = await AgentLoadHarness(context.agent_runtime).run(
        max_duration_ms=30_000,
        max_p95_latency_ms=5_000,
        max_error_rate=0,
        environment="CI",
    )

    assert run.status == "PASSED"
    assert run.target_agents == run.executed_agents == 300
    assert run.throughput_per_second > 0
    assert run.error_rate == 0
    assert run.live_execution_attempted is False
    assert all(run.invariants.values())


async def test_operational_evidence_is_idempotent_and_append_only(tmp_path):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'month10-evidence.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    snapshot, slo, alert, cost, run = _artifacts()

    assert await repository.save_operational_metric_snapshot(snapshot) == snapshot
    assert await repository.save_operational_metric_snapshot(snapshot) == snapshot
    assert await repository.save_slo_evaluations([slo]) == [slo]
    assert await repository.save_operational_alert_event(alert) == alert
    assert await repository.save_cost_usage_record(cost) == cost
    assert await repository.save_resilience_test_run(run) == run
    assert (await repository.list_operational_metric_snapshots())[0] == snapshot
    assert (await repository.list_slo_evaluations())[0] == slo
    assert (await repository.list_operational_alert_events())[0] == alert
    assert (await repository.list_cost_usage_records())[0] == cost
    assert (await repository.list_resilience_test_runs())[0] == run

    async with database.engine.begin() as connection:
        with pytest.raises(SQLAlchemyDatabaseError, match="append-only"):
            await connection.execute(
                text(
                    "UPDATE operational_metric_snapshots "
                    "SET registered_agents = 199"
                )
            )
    await database.dispose()


async def test_operations_apis_are_admin_only_and_never_inject_chaos(tmp_path):
    admin_key = "o" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'month10-api.db'}",
        AGENT_WORKER_ENABLED=False,
        BACKFILL_WORKER_ENABLED=False,
        OMS_WORKER_ENABLED=False,
        OMS_RECONCILIATION_ENABLED=False,
        OPERATIONS_MONITOR_ENABLED=False,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    headers = {"X-API-Key": admin_key}
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.get("/api/v1/operations/status")
            status = await client.get(
                "/api/v1/operations/status",
                headers=headers,
            )
            metrics = await client.get(
                "/api/v1/operations/metrics",
                headers=headers,
            )
            slos = await client.post(
                "/api/v1/operations/slos/evaluate",
                headers=headers,
            )
            chaos = await client.post(
                "/api/v1/operations/chaos",
                headers=headers,
            )

    assert denied.status_code == 401
    assert status.status_code == metrics.status_code == slos.status_code == 200
    assert status.json()["data"]["registered_agents"] == 300
    assert metrics.json()["data"]["snapshot"]["registered_agents"] == 300
    assert len(slos.json()["data"]["evaluations"]) == 4
    assert chaos.status_code == 404


def test_month10_contracts_and_private_migration_are_complete():
    manifest = json.loads(
        (CONTRACT_ROOT.parent.parent / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    names = (
        "operational-metric-snapshot.schema.json",
        "slo-evaluation.schema.json",
        "operational-alert-event.schema.json",
        "cost-usage-record.schema.json",
        "resilience-test-run.schema.json",
    )
    artifacts = _artifacts()

    assert len(manifest["schemas"]) == 56
    for name, artifact in zip(names, artifacts, strict=True):
        assert f"schemas/v1/{name}" in manifest["schemas"]
        schema = json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        ).validate(artifact.model_dump(mode="json"))

    migration = MIGRATION.read_text(encoding="utf-8").lower()
    tables = (
        "operational_metric_snapshots",
        "slo_evaluations",
        "operational_alert_events",
        "cost_usage_records",
        "resilience_test_runs",
    )
    for table in tables:
        assert f"create table capital_cipher.{table}" in migration
        assert f"alter table capital_cipher.{table}" in migration
        assert f"trg_{table}_immutable" in migration
    assert "security invoker" in migration
    assert "enable row level security" in migration
    assert "from anon" in migration
    assert "from authenticated" in migration
    assert "grant select" not in migration


def test_resilience_contract_rejects_live_execution_evidence():
    _, _, _, _, run = _artifacts()
    payload = run.model_dump()
    payload["live_execution_attempted"] = True
    with pytest.raises(ValueError):
        ResilienceTestRun.model_validate(payload)

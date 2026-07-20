"""Month 11 completion: prolonged validation of exactly 300 PAPER agents."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import text

from app.agents.month11_specialists import (
    MONTH11_SHADOW_VALIDATION_DEFINITIONS,
)
from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.schemas.agents import AgentInput
from app.schemas.common import AgentStatus, Exchange
from app.schemas.market import Candle
from app.schemas.oms import (
    ExecutionEnvironment,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from app.schemas.shadow_validation import ShadowCampaignDefinition
from app.shadow_validation.service import candle_dataset_fingerprint


def _campaign_candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(673):
        close = 100.0 * (1.0002**index) * (1 + 0.002 * ((index % 11) - 5))
        open_price = close * (0.999 if index % 2 else 1.001)
        candles.append(
            Candle(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                timeframe="15m",
                open=open_price,
                high=max(open_price, close) * 1.002,
                low=min(open_price, close) * 0.998,
                close=close,
                volume=100 + (index % 37),
                closed_at=start + timedelta(minutes=15 * index),
            )
        )
    return candles


def _definition(candles: list[Candle]) -> ShadowCampaignDefinition:
    return ShadowCampaignDefinition(
        replay_start_at=candles[0].closed_at,
        replay_end_at=candles[-1].closed_at,
        replay_candle_count=len(candles),
        checkpoint_interval_candles=96,
        dataset_fingerprint=candle_dataset_fingerprint(candles),
    )


def test_month11_catalog_adds_exactly_100_unique_read_only_specialists():
    definitions = MONTH11_SHADOW_VALIDATION_DEFINITIONS
    assert len(definitions) == 100
    assert len({item.name for item in definitions}) == 100
    assert len({item.capability for item in definitions}) == 100
    assert all(8 <= item.window <= 180 for item in definitions)


def test_runtime_has_exactly_300_paper_agents_with_bounded_authority():
    context = build_context(Settings(), with_database=False)
    registrations = context.agent_registry.registrations()
    month11_names = {
        item.name for item in MONTH11_SHADOW_VALIDATION_DEFINITIONS
    }
    assert len(registrations) == 300
    assert sum(item.decision_role == "PRIMARY" for item in registrations) == 3
    assert sum(item.decision_role == "SHADOW" for item in registrations) == 297
    assert month11_names.issubset({item.agent_name for item in registrations})
    assert all(item.execution_mode == "PAPER" for item in registrations)
    assert all(
        not set(item.capabilities)
        & {"submit-order", "cancel-order", "exchange-credentials"}
        for item in registrations
    )


@pytest.mark.asyncio
async def test_month11_specialists_are_deterministic_and_authority_free():
    context = build_context(Settings(), with_database=False)
    candles = _campaign_candles()[:400]
    for candle in candles:
        context.candle_store.add(candle)
    agent = context.agent_registry.get(
        MONTH11_SHADOW_VALIDATION_DEFINITIONS[0].name
    )
    request = AgentInput(
        request_id="month11-determinism",
        correlation_id="month11-determinism",
        agent_name=agent.name,
        timestamp=candles[-1].closed_at,
        symbol="BTCUSDT",
        timeframe="15m",
        market_context={"exchange": "BINANCE"},
    )
    first = await agent.run(request)
    second = await agent.run(request)
    assert first.status == second.status == AgentStatus.COMPLETED
    assert first.signal == second.signal
    assert first.confidence == second.confidence
    assert first.evidence == second.evidence
    assert first.evidence["decision_authority"] is False
    assert first.evidence["risk_authority"] is False
    assert first.evidence["order_authority"] is False


@pytest.mark.asyncio
async def test_seven_day_campaign_validates_all_month11_invariants():
    context = build_context(Settings(), with_database=False)
    candles = _campaign_candles()
    report = await context.shadow_validation_service.run(
        _definition(candles),
        candles,
    )
    checkpoints = context.shadow_validation_service.checkpoints(
        campaign_id=report.campaign.campaign_id,
        limit=100,
    )
    assert report.status == "PASSED"
    assert report.total_checkpoints == len(checkpoints) == 8
    assert report.executed_checkpoints == 6
    assert report.suspended_checkpoints == 2
    assert report.total_agent_executions == 1_800
    assert report.failed_agent_executions == 0
    assert report.reconciliation_runs == 8
    assert report.reconciliation_critical_mismatches == 0
    assert report.degradation_scenarios == ["BROKER", "DATABASE"]
    assert report.initial_risk_state_hash == report.final_risk_state_hash
    assert report.initial_order_count == report.final_order_count == 0
    assert report.initial_paper_trade_count == report.final_paper_trade_count == 0
    assert all(report.invariants.values())
    assert all(all(item.invariants.values()) for item in checkpoints)
    assert all(item.live_execution_attempted is False for item in checkpoints)


@pytest.mark.asyncio
async def test_critical_reconciliation_drift_fails_closed_with_evidence():
    context = build_context(Settings(), with_database=False)
    candles = _campaign_candles()
    calls = 0

    async def reconcile_with_first_critical_drift():
        nonlocal calls
        calls += 1
        if calls == 1:
            context.risk_manager.state.kill_switch_active = True
            return ReconciliationRun(
                exchange=Exchange.BINANCE,
                environment=ExecutionEnvironment.PAPER,
                status=ReconciliationRunStatus.DRIFT,
                mismatch_count=1,
                critical_mismatch_count=1,
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
            )
        return ReconciliationRun(
            exchange=Exchange.BINANCE,
            environment=ExecutionEnvironment.PAPER,
            status=ReconciliationRunStatus.MATCHED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )

    context.reconciliation_service.reconcile_once = (
        reconcile_with_first_critical_drift
    )
    report = await context.shadow_validation_service.run(
        _definition(candles),
        candles,
    )
    checkpoints = list(
        reversed(context.shadow_validation_service.checkpoints(limit=100))
    )
    assert report.status == "FAILED"
    assert report.executed_checkpoints == 0
    assert report.suspended_checkpoints == 8
    assert report.total_agent_executions == 0
    assert report.reconciliation_critical_mismatches == 1
    assert checkpoints[0].status == "BLOCKED_RECONCILIATION"
    assert all(item.executed_agents == 0 for item in checkpoints)
    assert all(item.acceptance_status == "FAILED" for item in checkpoints)
    assert report.invariants["risk_state_unchanged"] is False
    assert report.invariants["reconciliation_has_no_critical_drift"] is False


@pytest.mark.asyncio
async def test_shadow_evidence_is_immutable_and_round_trips(tmp_path):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'month11-evidence.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    context = build_context(Settings(), with_database=False)
    context.shadow_validation_service._repository = repository
    candles = _campaign_candles()
    report = await context.shadow_validation_service.run(
        _definition(candles),
        candles,
    )
    stored_reports = await repository.list_shadow_validation_reports()
    stored_checkpoints = await repository.list_shadow_campaign_checkpoints(
        campaign_id=report.campaign.campaign_id,
    )
    assert stored_reports == [report]
    assert len(stored_checkpoints) == 8
    with pytest.raises(Exception):
        async with database.session() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE shadow_validation_reports "
                    "SET status = 'FAILED' WHERE report_id = :report_id"
                ),
                {"report_id": report.report_id},
            )
    await database.dispose()


@pytest.mark.asyncio
async def test_shadow_validation_apis_are_admin_read_only(tmp_path):
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'month11-api.db'}",
        ADMIN_API_KEY="m" * 32,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.get(
                "/api/v1/operations/shadow-validation/reports"
            )
            reports = await client.get(
                "/api/v1/operations/shadow-validation/reports",
                headers={"X-API-Key": "m" * 32},
            )
            checkpoints = await client.get(
                "/api/v1/operations/shadow-validation/checkpoints",
                headers={"X-API-Key": "m" * 32},
            )
            start = await client.post(
                "/api/v1/operations/shadow-validation/run",
                headers={"X-API-Key": "m" * 32},
            )
    assert denied.status_code == 401
    assert reports.status_code == checkpoints.status_code == 200
    assert reports.json()["data"]["campaign_start_api_available"] is False
    assert checkpoints.json()["data"]["mutation_api_available"] is False
    assert start.status_code == 404
    await context.database.dispose()


def test_month11_contract_and_private_migration_are_complete():
    candles = _campaign_candles()
    definition = _definition(candles)
    assert len(definition.definition_hash) == 64
    contract_root = (
        Path(__file__).parents[3]
        / "packages"
        / "contracts"
    )
    manifest = json.loads(
        (contract_root / "manifest.json").read_text(encoding="utf-8")
    )
    names = (
        "shadow-campaign-definition.schema.json",
        "shadow-campaign-checkpoint.schema.json",
        "shadow-validation-report.schema.json",
    )
    assert len(manifest["schemas"]) == 52
    for name in names:
        assert f"schemas/v1/{name}" in manifest["schemas"]
        schema = json.loads(
            (contract_root / "schemas" / "v1" / name).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
    definition_schema = json.loads(
        (
            contract_root
            / "schemas"
            / "v1"
            / "shadow-campaign-definition.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(
        definition_schema,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(definition.model_dump(mode="json"))
    migration = (
        Path(__file__).parents[3]
        / "supabase"
        / "migrations"
        / "20260720210756_create_shadow_validation_campaigns.sql"
    ).read_text(encoding="utf-8")
    lowered = migration.lower()
    assert "capital_cipher.shadow_campaign_checkpoints" in lowered
    assert "capital_cipher.shadow_validation_reports" in lowered
    assert "enable row level security" in lowered
    assert "security invoker" in lowered
    assert "revoke all on all tables in schema capital_cipher from public" in lowered
    assert "reject_shadow_validation_mutation" in lowered
    assert "grant " not in lowered

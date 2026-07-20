"""Month 8 completion: 100 governed PAPER agents and honest evaluation."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError as SQLAlchemyDatabaseError

from app.agents.evaluation import (
    AgentEvaluationService,
    SpecialistEvidenceService,
)
from app.agents.month8_specialists import (
    DERIVATIVES_DEFINITIONS,
    EXTERNAL_DEFINITIONS,
    MACRO_DEFINITIONS,
    NEWS_DEFINITIONS,
    ONCHAIN_DEFINITIONS,
    TECHNICAL_DEFINITIONS,
    ExternalEvidenceSpecialist,
)
from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal, utcnow
from app.schemas.specialist_evaluation import SpecialistEvidence
from app.tests.conftest import make_candle, make_series

CONTRACT_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "contracts"
    / "schemas"
    / "v1"
)


def evidence(
    *,
    quality_score: int = 95,
    observed_at=None,
    received_at=None,
    value: float = 0.01,
    unit: str = "ratio",
    source_event_id: str = "funding-1",
) -> SpecialistEvidence:
    observed = observed_at or utcnow()
    return SpecialistEvidence(
        domain="DERIVATIVES",
        metric_name="funding_rate",
        scope="BTCUSDT",
        source="governed-test-adapter",
        source_event_id=source_event_id,
        value=value,
        unit=unit,
        quality_score=quality_score,
        observed_at=observed,
        received_at=received_at or observed,
        provenance_uri="urn:test:funding",
        payload_sha256=hashlib.sha256(source_event_id.encode()).hexdigest(),
    )


def test_month_8_catalog_is_exactly_60_new_specialists():
    assert len(TECHNICAL_DEFINITIONS) == 20
    assert len(DERIVATIVES_DEFINITIONS) == 15
    assert len(MACRO_DEFINITIONS) == 10
    assert len(ONCHAIN_DEFINITIONS) == 10
    assert len(NEWS_DEFINITIONS) == 5
    assert len(EXTERNAL_DEFINITIONS) == 40
    names = [
        item.name
        for item in (*TECHNICAL_DEFINITIONS, *EXTERNAL_DEFINITIONS)
    ]
    assert len(names) == len(set(names)) == 60


def test_runtime_cohort_is_100_paper_agents_and_new_agents_are_shadow_only():
    context = build_context(Settings(), with_database=False)
    registrations = context.agent_registry.registrations()
    month8_names = {
        item.name
        for item in (*TECHNICAL_DEFINITIONS, *EXTERNAL_DEFINITIONS)
    }
    assert len(registrations) == 100
    assert sum(item.decision_role == "PRIMARY" for item in registrations) == 3
    assert sum(item.decision_role == "SHADOW" for item in registrations) == 97
    assert month8_names.issubset(
        {item.agent_name for item in registrations}
    )
    assert all(
        item.execution_mode == "PAPER"
        and item.decision_role == "SHADOW"
        for item in registrations
        if item.agent_name in month8_names
    )
    for agent in context.agent_registry.shadow_agents():
        assert not hasattr(agent, "_risk")
        assert not hasattr(agent, "_engine")
        assert not hasattr(agent, "_credentials")


async def test_technical_specialists_are_deterministic_and_read_only():
    context = build_context(Settings(), with_database=False)
    for candle in make_series([100 * (1.001**i) for i in range(80)]):
        context.candle_store.add(candle)
    agent = context.agent_registry.get("ADXProxyAgent")
    agent_input = AgentInput(
        correlation_id="month8-technical",
        agent_name=agent.name,
        symbol="BTCUSDT",
        timeframe="15m",
        market_context={"exchange": "BINANCE"},
    )
    first = await agent.run(agent_input)
    second = await agent.run(agent_input)
    assert first.status == AgentStatus.COMPLETED
    assert first.signal == second.signal
    assert first.confidence == second.confidence
    assert first.evidence["metric"] == second.evidence["metric"]
    assert first.evidence["read_only"] is True


async def test_external_agent_missing_stale_and_low_quality_evidence_waits():
    service = SpecialistEvidenceService()
    definition = DERIVATIVES_DEFINITIONS[0]
    agent = ExternalEvidenceSpecialist(service, definition)
    now = utcnow()

    missing = await agent.run(
        AgentInput(
            correlation_id="month8-missing",
            agent_name=agent.name,
            timestamp=now,
            symbol="BTCUSDT",
            timeframe="15m",
        )
    )
    assert missing.signal == Signal.WAIT
    assert missing.confidence == 0
    assert missing.warnings == ["MISSING_EVIDENCE"]

    stale_at = now - timedelta(seconds=definition.max_age_seconds + 1)
    await service.ingest(
        evidence(
            observed_at=stale_at,
            received_at=stale_at,
            source_event_id="stale",
        )
    )
    stale = await agent.run(
        AgentInput(
            correlation_id="month8-stale",
            agent_name=agent.name,
            timestamp=now,
            symbol="BTCUSDT",
            timeframe="15m",
        )
    )
    assert stale.signal == Signal.WAIT
    assert stale.warnings == ["STALE_EVIDENCE"]

    await service.ingest(
        evidence(
            quality_score=79,
            observed_at=now,
            received_at=now,
            source_event_id="low-quality",
        )
    )
    low_quality = await agent.run(
        AgentInput(
            correlation_id="month8-low-quality",
            agent_name=agent.name,
            timestamp=now,
            symbol="BTCUSDT",
            timeframe="15m",
        )
    )
    assert low_quality.signal == Signal.WAIT
    assert low_quality.warnings == ["LOW_QUALITY_EVIDENCE"]


async def test_external_agent_uses_only_valid_governed_evidence():
    service = SpecialistEvidenceService()
    item = evidence()
    await service.ingest(item)
    agent = ExternalEvidenceSpecialist(service, DERIVATIVES_DEFINITIONS[0])
    output = await agent.run(
        AgentInput(
            correlation_id="month8-valid",
            agent_name=agent.name,
            timestamp=item.observed_at,
            symbol="BTCUSDT",
            timeframe="15m",
        )
    )
    assert output.status == AgentStatus.COMPLETED
    assert output.signal == Signal.SELL
    assert output.confidence > 0
    assert output.evidence["evidence_id"] == item.evidence_id
    assert output.evidence["read_only"] is True


async def test_external_agent_rejects_unit_mismatch_and_source_conflict():
    now = utcnow()
    service = SpecialistEvidenceService()
    await service.ingest(
        evidence(
            observed_at=now,
            received_at=now,
            unit="percent",
            source_event_id="wrong-unit",
        )
    )
    agent = ExternalEvidenceSpecialist(service, DERIVATIVES_DEFINITIONS[0])
    output = await agent.run(
        AgentInput(
            correlation_id="month8-unit",
            agent_name=agent.name,
            timestamp=now,
            symbol="BTCUSDT",
            timeframe="15m",
        )
    )
    assert output.signal == Signal.WAIT
    assert output.warnings == ["UNIT_MISMATCH"]

    duplicate_source = SpecialistEvidenceService()
    original = evidence(
        observed_at=now,
        received_at=now,
        source_event_id="immutable-source-event",
    )
    await duplicate_source.ingest(original)
    with pytest.raises(ValueError, match="Source event"):
        await duplicate_source.ingest(
            evidence(
                observed_at=now,
                received_at=now,
                value=0.02,
                source_event_id="immutable-source-event",
            )
        )


def test_specialist_evidence_rejects_unknown_fields_and_invalid_time():
    now = utcnow()
    payload = evidence().model_dump()
    payload["secret"] = "must-not-enter-evidence"
    with pytest.raises(PydanticValidationError):
        SpecialistEvidence.model_validate(payload)
    with pytest.raises(PydanticValidationError, match="received_at"):
        evidence(
            observed_at=now,
            received_at=now - timedelta(seconds=1),
        )


async def test_accuracy_and_leave_one_out_contribution_are_observational():
    context = build_context(Settings(), with_database=False)
    registrations = {
        item.agent_name: item
        for item in context.agent_registry.registrations()
        if item.agent_name in {"MomentumAgent", "VolatilityAgent"}
    }
    service = AgentEvaluationService(minimum_samples=30)
    start = utcnow()
    candle = make_candle(close=100, closed_at=start)
    outputs = [
        AgentOutput(
            agent_name="MomentumAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.BUY,
            confidence=100,
            reason="test helpful forecast",
        ),
        AgentOutput(
            agent_name="VolatilityAgent",
            status=AgentStatus.COMPLETED,
            signal=Signal.SELL,
            confidence=100,
            reason="test harmful forecast",
        ),
    ]
    outcomes, forecasts = await service.observe(
        candle=candle,
        correlation_id="month8-evaluation",
        outputs=outputs,
        registrations=registrations,
    )
    assert outcomes == []
    assert len(forecasts) == 2
    outcomes = await service.settle(
        make_candle(
            close=110,
            closed_at=start + timedelta(minutes=15),
        )
    )
    by_agent = {
        next(
            forecast.agent_name
            for forecast in forecasts
            if forecast.forecast_id == outcome.forecast_id
        ): outcome
        for outcome in outcomes
    }
    assert by_agent["MomentumAgent"].correct is True
    assert by_agent["MomentumAgent"].marginal_contribution > 0
    assert by_agent["VolatilityAgent"].correct is False
    assert by_agent["VolatilityAgent"].marginal_contribution < 0
    cards = await service.scorecards()
    assert all(card.status == "INSUFFICIENT_SAMPLE" for card in cards)
    documents = {
        "agent-forecast.schema.json": forecasts[0].model_dump(mode="json"),
        "agent-forecast-outcome.schema.json": outcomes[0].model_dump(
            mode="json"
        ),
        "agent-scorecard.schema.json": cards[0].model_dump(mode="json"),
    }
    for name, document in documents.items():
        validator = Draft202012Validator(
            json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        )
        assert list(validator.iter_errors(document)) == []


async def test_sqlite_persistence_is_idempotent_and_append_only(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'month8.db'}")
    await database.create_all()
    repository = Repository(database)
    service = SpecialistEvidenceService(repository)
    item = evidence()
    first = await service.ingest(item)
    repeated = await service.ingest(item)
    assert repeated == first
    validator = Draft202012Validator(
        json.loads(
            (CONTRACT_ROOT / "specialist-evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
    )
    assert list(validator.iter_errors(first.model_dump(mode="json"))) == []

    context = build_context(Settings(), with_database=False)
    registrations = {
        item.agent_name: item
        for item in context.agent_registry.registrations()
        if item.agent_name in {"MomentumAgent", "VolatilityAgent"}
    }
    evaluator = AgentEvaluationService(repository)
    start = utcnow()
    _, forecasts = await evaluator.observe(
        candle=make_candle(close=100, closed_at=start),
        correlation_id="month8-sqlite",
        outputs=[
            AgentOutput(
                agent_name=name,
                status=AgentStatus.COMPLETED,
                signal=signal,
                confidence=80,
                reason="SQLite persistence test",
            )
            for name, signal in (
                ("MomentumAgent", Signal.BUY),
                ("VolatilityAgent", Signal.SELL),
            )
        ],
        registrations=registrations,
    )
    outcomes = await evaluator.settle(
        make_candle(
            close=105,
            closed_at=start + timedelta(minutes=15),
        )
    )
    assert len(await repository.list_agent_forecasts()) == 2
    assert len(await repository.list_agent_forecast_outcomes()) == 2
    assert await repository.save_agent_forecasts(forecasts) == forecasts
    assert await repository.save_agent_forecast_outcomes(outcomes) == outcomes

    async with database.engine.begin() as connection:
        with pytest.raises(SQLAlchemyDatabaseError, match="append-only"):
            await connection.execute(
                text(
                    "UPDATE specialist_evidence "
                    "SET quality_score = 1 WHERE evidence_id = :evidence_id"
                ),
                {"evidence_id": item.evidence_id},
            )
    for table_name in ("agent_forecasts", "agent_forecast_outcomes"):
        async with database.engine.begin() as connection:
            with pytest.raises(SQLAlchemyDatabaseError, match="append-only"):
                await connection.execute(
                    text(f"DELETE FROM {table_name}")
                )
    await database.dispose()


async def test_evidence_and_evaluation_apis_are_admin_only(tmp_path):
    admin_key = "e" * 32
    settings = Settings(
        ADMIN_API_KEY=admin_key,
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'month8-api.db'}",
        AGENT_WORKER_ENABLED=False,
        BACKFILL_WORKER_ENABLED=False,
        OMS_WORKER_ENABLED=False,
        OMS_RECONCILIATION_ENABLED=False,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = ASGITransport(app=app)
    body = evidence().model_dump(mode="json")
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.post("/api/v1/agents/evidence", json=body)
            created = await client.post(
                "/api/v1/agents/evidence",
                json=body,
                headers={"X-API-Key": admin_key},
            )
            listed = await client.get(
                "/api/v1/agents/evidence",
                headers={"X-API-Key": admin_key},
            )
            scorecards = await client.get(
                "/api/v1/agents/evaluation/scorecards",
                headers={"X-API-Key": admin_key},
            )
    assert denied.status_code == 401
    assert created.status_code == 200
    assert listed.json()["data"]["evidence"][0]["evidence_id"] == (
        body["evidence_id"]
    )
    assert scorecards.json()["data"]["decision_authority"] is False


def test_month_8_contracts_and_private_migration_are_complete():
    manifest = json.loads(
        (CONTRACT_ROOT.parent.parent / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    names = (
        "specialist-evidence.schema.json",
        "agent-forecast.schema.json",
        "agent-forecast-outcome.schema.json",
        "agent-scorecard.schema.json",
    )
    assert len(manifest["schemas"]) == 39
    for name in names:
        assert f"schemas/v1/{name}" in manifest["schemas"]
        Draft202012Validator.check_schema(
            json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))
        )
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
        / "20260720155902_create_specialist_evidence_evaluation.sql"
    ).read_text(encoding="utf-8")
    assert "enable row level security" in migration.lower()
    assert "security invoker" in migration.lower()
    assert "from authenticated" in migration.lower()
    assert "from anon" in migration.lower()
    assert "reject_specialist_evaluation_mutation" in migration
    assert "live" not in migration.lower()

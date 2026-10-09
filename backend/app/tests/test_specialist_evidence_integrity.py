from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.evaluation import SpecialistEvidenceService
from app.schemas.specialist_evaluation import SpecialistEvidence


PAYLOAD_HASH = "a" * 64


def _evidence(*, value: float, evidence_id: str = "") -> SpecialistEvidence:
    observed_at = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    return SpecialistEvidence(
        evidence_id=evidence_id,
        domain="DERIVATIVES",
        metric_name="open_interest_change",
        scope="BTCUSDT",
        source="binance.usdm.public",
        source_event_id="BTCUSDT:open_interest_change:2026-10-09T15:00:00+00:00",
        value=value,
        unit="ratio",
        quality_score=95,
        observed_at=observed_at,
        received_at=observed_at + timedelta(seconds=1),
        provenance_uri="https://fapi.binance.com/futures/data/openInterestHist",
        payload_sha256=PAYLOAD_HASH,
    )


def test_specialist_evidence_identity_survives_numeric_38_18_roundtrip() -> None:
    original = _evidence(value=0.00012345678901234568)

    # PostgreSQL NUMERIC(38,18) stores this value at exactly 18 decimal places.
    persisted_value = float("0.000123456789012346")
    restored = _evidence(
        value=persisted_value,
        evidence_id=original.evidence_id,
    )

    assert original.value == persisted_value
    assert restored.evidence_id == original.evidence_id


@pytest.mark.asyncio
async def test_quarantine_preserves_valid_history_and_original_row(tmp_path):
    from sqlalchemy import select
    from app.database.models import SpecialistEvidenceModel, AuditLogModel
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'evidence.db'}")
    await database.create_all()
    repository = Repository(database)
    valid = _evidence(value=0.0001)
    await repository.save_specialist_evidence(valid)
    async with database.session() as session:
        values = valid.model_dump()
        values.update(evidence_id="0" * 64, source_event_id="corrupt-event")
        session.add(SpecialistEvidenceModel(**values))
        await session.commit()
    service = SpecialistEvidenceService(repository)
    try:
        await service.initialize()
        assert service.historical_integrity_rejected
        assert [e.evidence_id for e in await service.list(limit=10)] == [valid.evidence_id]
        await service.initialize()
        async with database.session() as session:
            assert await session.get(SpecialistEvidenceModel, "0" * 64) is not None
            audits = list(await session.scalars(select(AuditLogModel)))
            assert len(audits) == 1
            assert audits[0].entity_id == "0" * 64
            assert audits[0].audit_type == "SPECIALIST_EVIDENCE_QUARANTINED"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_funding_replay_after_restart_keeps_original_identity(tmp_path):
    from app.core.errors import ValidationError
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'funding.db'}")
    await database.create_all()
    repository = Repository(database)
    original = _evidence(value=0.0001)
    replay_data = original.model_dump(exclude={"evidence_id"})
    replay_data["received_at"] += timedelta(hours=1)
    replay = SpecialistEvidence(**replay_data)
    try:
        await repository.save_specialist_evidence(original)
        stored = await repository.save_specialist_evidence(replay)
        assert stored == original
        replay_data["value"] = 0.001
        with pytest.raises(ValidationError, match="different evidence"):
            await repository.save_specialist_evidence(SpecialistEvidence(**replay_data))
    finally:
        await database.engine.dispose()


class UnavailableRepository:
    async def list_specialist_evidence(self, *, limit: int):
        raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_database_availability_failure_still_fails_initialization() -> None:
    service = SpecialistEvidenceService(UnavailableRepository())

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.initialize()


@pytest.mark.asyncio
async def test_postgres_evidence_numeric_boundary_and_fractional_latency():
    import os
    from decimal import Decimal
    from uuid import uuid4
    from sqlalchemy import text
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    url = os.environ.get("POSTGRES_TEST_URL")
    if not url:
        pytest.skip("POSTGRES_TEST_URL is not configured")
    database = Database(url)
    repository = Repository(database)
    try:
        for value in (0.00012345678901234568, 0.12345678901234568, -0.0000123456789012345):
            data = _evidence(value=value).model_dump(exclude={"evidence_id"})
            data["source_event_id"] = str(uuid4())
            evidence = SpecialistEvidence(**data)
            await repository.save_specialist_evidence(evidence)
            assert await repository.save_specialist_evidence(evidence) == evidence
            async with database.session() as session:
                restored = await session.scalar(text("SELECT CAST(:value AS numeric(38,18))"), {"value": Decimal(str(evidence.value))})
                assert float(restored) == evidence.value
        async with database.session() as session:
            column_type = await session.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_schema='capital_cipher' AND table_name='agent_outputs' AND column_name='latency_ms'"))
            assert column_type == "double precision"
    finally:
        await database.engine.dispose()


@pytest.mark.asyncio
async def test_agent_reports_sub_millisecond_duration(monkeypatch):
    from types import SimpleNamespace
    from app.agents.base import BaseAgent
    from app.schemas.agents import AgentInput
    from app.schemas.common import AgentStatus, Signal
    class FastAgent(BaseAgent):
        name = "FastLatencyAgent"
        async def _analyze(self, agent_input):
            return self._output(AgentStatus.COMPLETED, Signal.HOLD, 50, "test")
    times = iter([10.0, 10.0001, 10.0004, 10.0005])
    monkeypatch.setattr("app.agents.base.time", SimpleNamespace(monotonic=lambda: next(times)))
    agent = FastAgent()
    output = await agent.run(AgentInput(correlation_id="latency-test", agent_name=agent.name, symbol="BTCUSDT", timeframe="15m"))
    assert output.latency_ms == pytest.approx(0.4)
    assert 0 < output.latency_ms < 1

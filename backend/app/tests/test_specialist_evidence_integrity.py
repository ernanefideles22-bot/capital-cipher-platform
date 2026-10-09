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


class CorruptHistoricalRepository:
    async def list_specialist_evidence(self, *, limit: int):
        assert limit == 10_000
        # Simulate one legacy row whose pre-persistence float generated an ID
        # that can no longer be reconstructed from NUMERIC(38,18).
        _evidence(
            value=0.000123456789012346,
            evidence_id="0" * 64,
        )
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_corrupt_historical_shadow_evidence_is_excluded_not_trusted() -> None:
    service = SpecialistEvidenceService(CorruptHistoricalRepository())

    await service.initialize()

    assert service.historical_integrity_rejected is True
    assert await service.list(limit=10) == []


class UnavailableRepository:
    async def list_specialist_evidence(self, *, limit: int):
        raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_database_availability_failure_still_fails_initialization() -> None:
    service = SpecialistEvidenceService(UnavailableRepository())

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.initialize()

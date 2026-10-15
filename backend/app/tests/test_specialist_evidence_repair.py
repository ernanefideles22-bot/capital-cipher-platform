from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agents.evaluation import SpecialistEvidenceService
from app.database.repositories.repository import Repository
from app.schemas.specialist_evaluation import SpecialistEvidence


def _evidence(*, value: float = 0.001) -> SpecialistEvidence:
    observed = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    return SpecialistEvidence(
        domain="DERIVATIVES",
        metric_name="funding_rate",
        scope="ETHUSDT",
        source="binance.usdm.public",
        source_event_id="funding:ETHUSDT:20261009T080000Z",
        value=value,
        unit="ratio",
        quality_score=100,
        observed_at=observed,
        received_at=observed + timedelta(seconds=1),
        provenance_uri="https://fapi.binance.com/fapi/v1/fundingRate",
        payload_sha256="a" * 64,
    )


def _with_received_at(evidence: SpecialistEvidence, received_at: datetime) -> SpecialistEvidence:
    payload = evidence.model_dump(exclude={"evidence_id"})
    payload["received_at"] = received_at
    return SpecialistEvidence(**payload)


def test_repeated_source_event_ignores_receipt_timestamp_for_idempotency() -> None:
    first = _evidence()
    repeated = _with_received_at(
        first,
        first.received_at + timedelta(minutes=5),
    )

    assert repeated.evidence_id != first.evidence_id
    assert Repository._same_specialist_source_event(first, repeated) is True


def test_fresh_provider_event_can_confirm_matching_legacy_row_without_hash() -> None:
    incoming = _evidence()
    row = SimpleNamespace(
        schema_version=incoming.schema_version,
        domain=incoming.domain,
        metric_name=incoming.metric_name,
        scope=incoming.scope,
        source=incoming.source,
        source_event_id=incoming.source_event_id,
        value=incoming.value,
        unit=incoming.unit,
        quality_score=incoming.quality_score,
        observed_at=incoming.observed_at,
        provenance_uri=incoming.provenance_uri,
        payload_sha256=incoming.payload_sha256,
    )

    assert Repository._same_specialist_source_row(row, incoming) is True
    row.value = incoming.value + 0.01
    assert Repository._same_specialist_source_row(row, incoming) is False


class MixedIntegrityRepository:
    def __init__(self) -> None:
        self.valid = _evidence(value=0.002)

    async def list_specialist_evidence_with_rejections(self, *, limit: int):
        assert limit == 10_000
        return [self.valid], [
            {
                "evidence_id": "0" * 64,
                "source": "binance.usdm.public",
                "source_event_id": "legacy-corrupt-event",
                "error_type": "ValidationError",
                "error": "evidence_id does not match immutable evidence",
            }
        ]


@pytest.mark.asyncio
async def test_invalid_history_row_does_not_drop_valid_history() -> None:
    repository = MixedIntegrityRepository()
    service = SpecialistEvidenceService(repository)

    await service.initialize()

    assert service.historical_integrity_rejected is True
    assert await service.list(limit=10) == [repository.valid]

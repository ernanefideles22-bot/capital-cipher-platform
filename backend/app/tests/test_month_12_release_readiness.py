"""Month 12: independent evidence, fail-closed gate and rollback rehearsal."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from app.api.context import build_context
from app.core.config import Settings
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.main import create_app
from app.release_readiness.audit import ReleaseAuditRunner
from app.release_readiness.service import ReleaseReadinessService
from app.schemas.common import utcnow
from app.schemas.release_readiness import (
    IndependentAuditAttestation,
    ReleaseEvidenceBundle,
    ReleaseGateDecision,
    TestnetCanaryLimits as CanaryLimits,
)

ROOT = Path(__file__).resolve().parents[3]
SOURCE_REVISION = "395ddd99c9c73cae71eaaba1b856d867231ab1d3"


def _evidence(*, ci_passed: bool = True) -> ReleaseEvidenceBundle:
    return ReleaseAuditRunner(ROOT).collect(
        source_revision=SOURCE_REVISION,
        month11_report_id="month11-approved-report",
        month11_report_sha256="a" * 64,
        ci_quality_gate_passed=ci_passed,
        database_migrations_validated=True,
    )


def _attestation(
    evidence: ReleaseEvidenceBundle,
    *,
    decision: str = "APPROVED_TESTNET",
) -> IndependentAuditAttestation:
    issued_at = utcnow()
    return IndependentAuditAttestation(
        evidence_bundle_id=evidence.evidence_bundle_id,
        evidence_bundle_sha256=evidence.bundle_sha256,
        source_revision=evidence.source_revision,
        reviewer_id="external-security-reviewer-001",
        reviewer_organization="Independent Audit Laboratory",
        decision=decision,
        signed_artifact_sha256="b" * 64,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=7),
    )


def test_repository_audit_is_reproducible_and_passes_complete_matrix():
    first = _evidence()
    second = _evidence()
    assert first.status == second.status == "PASSED"
    assert first.source_tree_sha256 == second.source_tree_sha256
    assert len(first.checks) == 10
    assert {item.status for item in first.checks} == {"PASSED"}
    assert first.contract_count == 56
    assert first.live_execution_surface_present is False
    assert len(first.bundle_sha256) == 64


async def test_gate_stays_blocked_without_real_external_attestation():
    evidence = _evidence()
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    decision = await service.evaluate_gate(evidence)
    assert decision.outcome == "BLOCKED_PENDING_EXTERNAL_AUDIT"
    assert decision.testnet_release_authorized is False
    assert decision.runtime_configuration_changed is False
    assert decision.live_execution_authorized is False
    assert decision.expires_at is None


async def test_failed_technical_evidence_cannot_be_promoted():
    evidence = _evidence(ci_passed=False)
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    attestation = _attestation(evidence)
    await service.record_external_attestation(attestation)
    canary = await service.run_local_testnet_canary(evidence, attestation)
    decision = await service.evaluate_gate(
        evidence,
        attestation=attestation,
        canary=canary,
    )
    assert evidence.status == "FAILED"
    assert canary.status == "FAILED"
    assert decision.outcome == "BLOCKED_TECHNICAL"
    assert decision.testnet_release_authorized is False


async def test_external_attestation_canary_and_rollback_are_strictly_bounded():
    evidence = _evidence()
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    attestation = _attestation(evidence)
    await service.record_external_attestation(attestation)
    canary = await service.run_local_testnet_canary(evidence, attestation)
    decision = await service.evaluate_gate(
        evidence,
        attestation=attestation,
        canary=canary,
    )
    assert canary.status == "PASSED"
    assert canary.transport == "LOCAL_NO_NETWORK_REHEARSAL"
    assert canary.limits.max_virtual_notional_usd == 100
    assert canary.limits.max_orders == canary.limits.max_open_positions == 1
    assert canary.limits.leverage == 1
    assert canary.limits.withdrawal_permission is False
    assert canary.remote_api_call_attempted is False
    assert canary.real_funds_used is False
    assert canary.kill_switch_triggered is True
    assert canary.canary_canceled is True
    assert canary.reconciliation_flat is True
    assert canary.rollback_completed is True
    assert decision.outcome == "APPROVED_TESTNET"
    assert decision.testnet_release_authorized is True
    assert decision.runtime_configuration_changed is False
    assert decision.live_execution_authorized is False
    assert decision.expires_at <= decision.decided_at + timedelta(hours=24)


async def test_rejected_or_mismatched_attestation_fails_closed():
    evidence = _evidence()
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    rejected = _attestation(evidence, decision="REJECTED")
    await service.record_external_attestation(rejected)
    drill = await service.run_local_testnet_canary(evidence, rejected)
    assert drill.status == "FAILED"
    decision = await service.evaluate_gate(
        evidence,
        attestation=rejected,
        canary=drill,
    )
    assert decision.outcome == "BLOCKED_TECHNICAL"
    mismatched = rejected.model_copy(
        update={"evidence_bundle_sha256": "c" * 64}
    )
    with pytest.raises(ValueError, match="does not match"):
        await service.record_external_attestation(mismatched)


async def test_unrecorded_attestation_cannot_run_canary_or_approve_gate():
    evidence = _evidence()
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    unrecorded = _attestation(evidence)
    with pytest.raises(ValueError, match="recorded immutable evidence"):
        await service.run_local_testnet_canary(evidence, unrecorded)
    decision = await service.evaluate_gate(
        evidence,
        attestation=unrecorded,
    )
    assert decision.outcome == "BLOCKED_TECHNICAL"
    assert decision.criteria["external_audit_recorded"] is False
    assert decision.testnet_release_authorized is False


def test_contracts_reject_unsafe_limits_and_unverified_reviewers():
    with pytest.raises(PydanticValidationError):
        CanaryLimits(max_virtual_notional_usd=100.01)
    evidence = _evidence()
    payload = _attestation(evidence).model_dump(mode="json")
    payload["reviewer_kind"] = "INTERNAL"
    with pytest.raises(PydanticValidationError):
        IndependentAuditAttestation.model_validate(payload)
    with pytest.raises(PydanticValidationError, match="exact criteria matrix"):
        ReleaseGateDecision(
            evidence_bundle_id=evidence.evidence_bundle_id,
            source_revision=evidence.source_revision,
            criteria={f"unsafe_{index}": False for index in range(10)},
            outcome="BLOCKED_TECHNICAL",
            testnet_release_authorized=False,
        )


async def test_release_evidence_round_trips_across_repository_restart(tmp_path):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'month12-release.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    first = ReleaseReadinessService(repository)
    evidence = await first.record_evidence(_evidence())
    attestation = await first.record_external_attestation(
        _attestation(evidence)
    )
    drill = await first.run_local_testnet_canary(evidence, attestation)
    decision = await first.evaluate_gate(
        evidence,
        attestation=attestation,
        canary=drill,
    )
    restored = ReleaseReadinessService(repository)
    await restored.initialize()
    assert restored.evidence_bundles() == [evidence]
    assert restored.attestations() == [attestation]
    assert restored.canary_drills() == [drill]
    assert restored.gate_decisions() == [decision]
    await database.dispose()


async def test_release_readiness_apis_are_admin_and_read_only(tmp_path):
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'month12-api.db'}",
        ADMIN_API_KEY="r" * 32,
    )
    context = build_context(settings, with_database=True)
    app = create_app(context, with_market_data=False)
    transport = httpx.ASGITransport(app=app)
    paths = (
        "evidence",
        "attestations",
        "canary-drills",
        "gates",
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            denied = await client.get(
                "/api/v1/operations/release-readiness/gates"
            )
            responses = [
                await client.get(
                    f"/api/v1/operations/release-readiness/{path}",
                    headers={"X-API-Key": "r" * 32},
                )
                for path in paths
            ]
            mutation = await client.post(
                "/api/v1/operations/release-readiness/gates/evaluate",
                headers={"X-API-Key": "r" * 32},
            )
    assert denied.status_code == 401
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["data"]["mutation_api_available"] is False
    assert responses[2].json()["data"]["remote_testnet_call_available"] is False
    assert responses[3].json()["data"]["runtime_activation_api_available"] is False
    assert mutation.status_code == 404
    await context.database.dispose()


async def test_month12_contracts_and_private_migration_are_complete():
    contract_root = ROOT / "packages" / "contracts"
    manifest = json.loads(
        (contract_root / "manifest.json").read_text(encoding="utf-8")
    )
    names = (
        "release-evidence-bundle.schema.json",
        "independent-audit-attestation.schema.json",
        "testnet-canary-drill-report.schema.json",
        "release-gate-decision.schema.json",
    )
    assert len(manifest["schemas"]) == 56
    for name in names:
        assert f"schemas/v1/{name}" in manifest["schemas"]
        schema = json.loads(
            (contract_root / "schemas" / "v1" / name).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
    evidence = _evidence()
    service = ReleaseReadinessService()
    await service.record_evidence(evidence)
    attestation = await service.record_external_attestation(
        _attestation(evidence)
    )
    canary = await service.run_local_testnet_canary(evidence, attestation)
    decision = await service.evaluate_gate(
        evidence,
        attestation=attestation,
        canary=canary,
    )
    instances = {
        "release-evidence-bundle.schema.json": evidence,
        "independent-audit-attestation.schema.json": attestation,
        "testnet-canary-drill-report.schema.json": canary,
        "release-gate-decision.schema.json": decision,
    }
    for name, instance in instances.items():
        schema = json.loads(
            (contract_root / "schemas" / "v1" / name).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(
            schema,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        ).validate(instance.model_dump(mode="json"))
    migration = (
        ROOT
        / "supabase/migrations/"
        "20260720215457_create_release_readiness_attestations.sql"
    ).read_text(encoding="utf-8").lower()
    for table in (
        "release_evidence_bundles",
        "independent_audit_attestations",
        "testnet_canary_drill_reports",
        "release_gate_decisions",
    ):
        assert f"capital_cipher.{table}" in migration
    assert "enable row level security" in migration
    assert "security invoker" in migration
    assert "reject_release_readiness_mutation" in migration
    assert "revoke all on all tables in schema capital_cipher from public" in migration
    assert "grant " not in migration


def test_live_execution_remains_absent_from_runtime():
    oms_schema = (ROOT / "backend/app/schemas/oms.py").read_text(
        encoding="utf-8"
    )
    context = (ROOT / "backend/app/api/context.py").read_text(
        encoding="utf-8"
    )
    assert 'LIVE = "LIVE"' not in oms_schema
    assert "ExecutionEnvironment.LIVE" not in context
    settings = Settings()
    assert settings.system_mode == "PAPER"
    assert settings.oms_execution_environment == "PAPER"

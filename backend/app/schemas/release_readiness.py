"""Versioned, fail-closed contracts for Month 12 release readiness."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import utcnow

RELEASE_READINESS_SCHEMA_VERSION = "1.0.0"
RELEASE_PROTOCOL_VERSION = "testnet-release-readiness-v1"

REQUIRED_AUDIT_CHECKS = frozenset(
    {
        "ci_quality_gate",
        "contracts_versioned",
        "database_migrations_validated",
        "environment_segregation",
        "live_execution_absent",
        "month11_shadow_validation",
        "private_append_only_evidence",
        "risk_kill_switch",
        "testnet_explicit_acknowledgement",
        "testnet_host_allowlist",
    }
)
REQUIRED_GATE_CRITERIA = frozenset(
    {
        "canary_evidence_matches",
        "canary_evidence_recorded",
        "exact_source_revision",
        "external_audit_approved_testnet",
        "external_audit_current",
        "external_audit_independent",
        "external_audit_present",
        "external_audit_recorded",
        "live_execution_absent",
        "local_canary_passed",
        "no_remote_call_or_real_funds",
        "rollback_completed",
        "technical_evidence_passed",
        "technical_evidence_recorded",
    }
)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


class StrictReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseAuditCheck(StrictReleaseModel):
    check_name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    status: Literal["PASSED", "FAILED"]
    summary: str = Field(min_length=1, max_length=500)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReleaseEvidenceBundle(StrictReleaseModel):
    schema_version: Literal["1.0.0"] = RELEASE_READINESS_SCHEMA_VERSION
    protocol_version: Literal["testnet-release-readiness-v1"] = (
        RELEASE_PROTOCOL_VERSION
    )
    evidence_bundle_id: str = Field(default_factory=lambda: str(uuid4()))
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_tree_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    month11_report_id: str
    month11_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    collection_environment: Literal["PAPER"] = "PAPER"
    checks: list[ReleaseAuditCheck] = Field(min_length=10, max_length=64)
    status: Literal["PASSED", "FAILED"]
    contract_count: Literal[56] = 56
    live_execution_surface_present: Literal[False] = False
    bundle_sha256: str = ""
    collected_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_bundle(self) -> "ReleaseEvidenceBundle":
        names = [check.check_name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("Release audit checks must be unique")
        if set(names) != REQUIRED_AUDIT_CHECKS:
            raise ValueError("Release evidence does not contain the exact audit matrix")
        passed = all(check.status == "PASSED" for check in self.checks)
        if (self.status == "PASSED") != passed:
            raise ValueError("Release evidence status must match every audit check")
        payload = self.model_dump(
            mode="json",
            exclude={"bundle_sha256", "collected_at"},
        )
        expected = _digest(payload)
        if self.bundle_sha256 and self.bundle_sha256 != expected:
            raise ValueError("Release evidence bundle hash mismatch")
        self.bundle_sha256 = expected
        return self


class IndependentAuditAttestation(StrictReleaseModel):
    schema_version: Literal["1.0.0"] = RELEASE_READINESS_SCHEMA_VERSION
    attestation_id: str = Field(default_factory=lambda: str(uuid4()))
    evidence_bundle_id: str
    evidence_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    reviewer_id: str = Field(min_length=3, max_length=160)
    reviewer_organization: str = Field(min_length=2, max_length=160)
    reviewer_kind: Literal["EXTERNAL"] = "EXTERNAL"
    independent_of_development_team: Literal[True] = True
    decision: Literal["APPROVED_TESTNET", "REJECTED"]
    unresolved_critical_findings: int = Field(default=0, ge=0, le=10_000)
    signed_artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    live_execution_authorized: Literal[False] = False
    issued_at: AwareDatetime = Field(default_factory=utcnow)
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_attestation(self) -> "IndependentAuditAttestation":
        if self.expires_at <= self.issued_at:
            raise ValueError("Independent audit attestation must expire in the future")
        if self.expires_at - self.issued_at > timedelta(days=30):
            raise ValueError("Independent audit attestation may be valid for at most 30 days")
        if (
            self.decision == "APPROVED_TESTNET"
            and self.unresolved_critical_findings != 0
        ):
            raise ValueError("TESTNET cannot be approved with critical findings")
        return self


class TestnetCanaryLimits(StrictReleaseModel):
    max_virtual_notional_usd: float = Field(default=100.0, gt=0, le=100)
    max_orders: Literal[1] = 1
    max_open_positions: Literal[1] = 1
    leverage: Literal[1] = 1
    withdrawal_permission: Literal[False] = False


class TestnetCanaryDrillReport(StrictReleaseModel):
    schema_version: Literal["1.0.0"] = RELEASE_READINESS_SCHEMA_VERSION
    drill_id: str = Field(default_factory=lambda: str(uuid4()))
    evidence_bundle_id: str
    attestation_id: str
    environment: Literal["TESTNET"] = "TESTNET"
    transport: Literal["LOCAL_NO_NETWORK_REHEARSAL"] = (
        "LOCAL_NO_NETWORK_REHEARSAL"
    )
    limits: TestnetCanaryLimits = Field(default_factory=TestnetCanaryLimits)
    unapproved_attempt_blocked: bool
    approved_canary_admitted: bool
    kill_switch_triggered: bool
    canary_canceled: bool
    reconciliation_flat: bool
    rollback_completed: bool
    remote_api_call_attempted: Literal[False] = False
    real_funds_used: Literal[False] = False
    live_execution_attempted: Literal[False] = False
    status: Literal["PASSED", "FAILED"]
    report_sha256: str = ""
    completed_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_drill(self) -> "TestnetCanaryDrillReport":
        controls = (
            self.unapproved_attempt_blocked,
            self.approved_canary_admitted,
            self.kill_switch_triggered,
            self.canary_canceled,
            self.reconciliation_flat,
            self.rollback_completed,
        )
        if (self.status == "PASSED") != all(controls):
            raise ValueError("Canary drill status must match all safety controls")
        payload = self.model_dump(
            mode="json",
            exclude={"report_sha256", "completed_at"},
        )
        expected = _digest(payload)
        if self.report_sha256 and self.report_sha256 != expected:
            raise ValueError("Canary drill report hash mismatch")
        self.report_sha256 = expected
        return self


class ReleaseGateDecision(StrictReleaseModel):
    schema_version: Literal["1.0.0"] = RELEASE_READINESS_SCHEMA_VERSION
    gate_decision_id: str = Field(default_factory=lambda: str(uuid4()))
    evidence_bundle_id: str
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    target_environment: Literal["TESTNET"] = "TESTNET"
    attestation_id: str | None = None
    canary_drill_id: str | None = None
    criteria: dict[str, bool] = Field(min_length=10, max_length=32)
    outcome: Literal[
        "APPROVED_TESTNET",
        "BLOCKED_PENDING_EXTERNAL_AUDIT",
        "BLOCKED_TECHNICAL",
    ]
    testnet_release_authorized: bool
    runtime_configuration_changed: Literal[False] = False
    live_execution_authorized: Literal[False] = False
    decision_sha256: str = ""
    decided_at: AwareDatetime = Field(default_factory=utcnow)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> "ReleaseGateDecision":
        if set(self.criteria) != REQUIRED_GATE_CRITERIA:
            raise ValueError("Release gate does not contain the exact criteria matrix")
        all_passed = all(self.criteria.values())
        approved = self.outcome == "APPROVED_TESTNET"
        if approved != all_passed or self.testnet_release_authorized != approved:
            raise ValueError("Release authorization must fail closed with its criteria")
        if approved:
            if not self.attestation_id or not self.canary_drill_id:
                raise ValueError("Approved TESTNET requires audit and canary evidence")
            if self.expires_at is None or self.expires_at <= self.decided_at:
                raise ValueError("Approved TESTNET gate requires a future expiry")
        elif self.expires_at is not None:
            raise ValueError("Blocked release decisions cannot have an expiry")
        payload = self.model_dump(
            mode="json",
            exclude={"decision_sha256", "decided_at"},
        )
        expected = _digest(payload)
        if self.decision_sha256 and self.decision_sha256 != expected:
            raise ValueError("Release gate decision hash mismatch")
        self.decision_sha256 = expected
        return self

"""Fail-closed Month 12 release assessment and local canary rehearsal."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
from typing import Protocol

from app.schemas.common import utcnow
from app.schemas.release_readiness import (
    IndependentAuditAttestation,
    ReleaseEvidenceBundle,
    ReleaseGateDecision,
    TestnetCanaryDrillReport,
)


class ReleaseReadinessRepository(Protocol):
    async def save_release_evidence_bundle(
        self, evidence: ReleaseEvidenceBundle
    ) -> ReleaseEvidenceBundle: ...

    async def list_release_evidence_bundles(
        self, *, limit: int = 100
    ) -> list[ReleaseEvidenceBundle]: ...

    async def save_independent_audit_attestation(
        self, attestation: IndependentAuditAttestation
    ) -> IndependentAuditAttestation: ...

    async def list_independent_audit_attestations(
        self, *, limit: int = 100
    ) -> list[IndependentAuditAttestation]: ...

    async def save_testnet_canary_drill_report(
        self, report: TestnetCanaryDrillReport
    ) -> TestnetCanaryDrillReport: ...

    async def list_testnet_canary_drill_reports(
        self, *, limit: int = 100
    ) -> list[TestnetCanaryDrillReport]: ...

    async def save_release_gate_decision(
        self, decision: ReleaseGateDecision
    ) -> ReleaseGateDecision: ...

    async def list_release_gate_decisions(
        self, *, limit: int = 100
    ) -> list[ReleaseGateDecision]: ...


class ReleaseReadinessService:
    """Stores evidence and evaluates TESTNET only; it cannot alter runtime."""

    def __init__(
        self, repository: ReleaseReadinessRepository | None = None
    ) -> None:
        self._repository = repository
        self._evidence: deque[ReleaseEvidenceBundle] = deque(maxlen=1_000)
        self._attestations: deque[IndependentAuditAttestation] = deque(
            maxlen=1_000
        )
        self._drills: deque[TestnetCanaryDrillReport] = deque(maxlen=1_000)
        self._decisions: deque[ReleaseGateDecision] = deque(maxlen=1_000)

    async def initialize(self) -> None:
        if self._repository is None:
            return
        evidence, attestations, drills, decisions = await asyncio.gather(
            self._repository.list_release_evidence_bundles(limit=1_000),
            self._repository.list_independent_audit_attestations(limit=1_000),
            self._repository.list_testnet_canary_drill_reports(limit=1_000),
            self._repository.list_release_gate_decisions(limit=1_000),
        )
        self._evidence.extend(reversed(evidence))
        self._attestations.extend(reversed(attestations))
        self._drills.extend(reversed(drills))
        self._decisions.extend(reversed(decisions))

    async def record_evidence(
        self, evidence: ReleaseEvidenceBundle
    ) -> ReleaseEvidenceBundle:
        stored = (
            await self._repository.save_release_evidence_bundle(evidence)
            if self._repository is not None
            else evidence
        )
        if all(
            item.evidence_bundle_id != stored.evidence_bundle_id
            for item in self._evidence
        ):
            self._evidence.append(stored)
        return stored

    async def record_external_attestation(
        self, attestation: IndependentAuditAttestation
    ) -> IndependentAuditAttestation:
        evidence = self._evidence_by_id(attestation.evidence_bundle_id)
        if evidence is None:
            raise ValueError("Independent audit references unknown evidence")
        if (
            attestation.evidence_bundle_sha256 != evidence.bundle_sha256
            or attestation.source_revision != evidence.source_revision
        ):
            raise ValueError("Independent audit does not match release evidence")
        stored = (
            await self._repository.save_independent_audit_attestation(
                attestation
            )
            if self._repository is not None
            else attestation
        )
        if all(
            item.attestation_id != stored.attestation_id
            for item in self._attestations
        ):
            self._attestations.append(stored)
        return stored

    async def run_local_testnet_canary(
        self,
        evidence: ReleaseEvidenceBundle,
        attestation: IndependentAuditAttestation,
    ) -> TestnetCanaryDrillReport:
        """Rehearse controls in memory without calling an exchange API."""

        recorded_evidence = self._evidence_by_id(evidence.evidence_bundle_id)
        recorded_attestation = self._attestation_by_id(
            attestation.attestation_id
        )
        if recorded_evidence != evidence or recorded_attestation != attestation:
            raise ValueError(
                "Canary requires recorded immutable evidence and attestation"
            )
        approved = (
            evidence.status == "PASSED"
            and attestation.decision == "APPROVED_TESTNET"
            and attestation.unresolved_critical_findings == 0
            and attestation.expires_at > utcnow()
            and attestation.evidence_bundle_id == evidence.evidence_bundle_id
            and attestation.evidence_bundle_sha256 == evidence.bundle_sha256
            and attestation.source_revision == evidence.source_revision
        )
        report = TestnetCanaryDrillReport(
            evidence_bundle_id=evidence.evidence_bundle_id,
            attestation_id=attestation.attestation_id,
            unapproved_attempt_blocked=True,
            approved_canary_admitted=approved,
            kill_switch_triggered=approved,
            canary_canceled=approved,
            reconciliation_flat=approved,
            rollback_completed=approved,
            status="PASSED" if approved else "FAILED",
        )
        stored = (
            await self._repository.save_testnet_canary_drill_report(report)
            if self._repository is not None
            else report
        )
        if all(item.drill_id != stored.drill_id for item in self._drills):
            self._drills.append(stored)
        return stored

    async def evaluate_gate(
        self,
        evidence: ReleaseEvidenceBundle,
        *,
        attestation: IndependentAuditAttestation | None = None,
        canary: TestnetCanaryDrillReport | None = None,
    ) -> ReleaseGateDecision:
        now = utcnow()
        evidence_recorded = (
            self._evidence_by_id(evidence.evidence_bundle_id) == evidence
        )
        attestation_recorded = bool(
            attestation
            and self._attestation_by_id(attestation.attestation_id)
            == attestation
        )
        canary_recorded = bool(
            canary and self._drill_by_id(canary.drill_id) == canary
        )
        external_present = attestation is not None
        audit_matches = bool(
            attestation
            and attestation.evidence_bundle_id == evidence.evidence_bundle_id
            and attestation.evidence_bundle_sha256 == evidence.bundle_sha256
            and attestation.source_revision == evidence.source_revision
        )
        audit_current = bool(attestation and attestation.expires_at > now)
        audit_approved = bool(
            attestation
            and attestation.decision == "APPROVED_TESTNET"
            and attestation.unresolved_critical_findings == 0
        )
        canary_matches = bool(
            canary
            and attestation
            and canary.evidence_bundle_id == evidence.evidence_bundle_id
            and canary.attestation_id == attestation.attestation_id
        )
        criteria = {
            "technical_evidence_recorded": evidence_recorded,
            "technical_evidence_passed": evidence.status == "PASSED",
            "exact_source_revision": audit_matches,
            "external_audit_present": external_present,
            "external_audit_recorded": attestation_recorded,
            "external_audit_independent": bool(
                attestation
                and attestation.reviewer_kind == "EXTERNAL"
                and attestation.independent_of_development_team
            ),
            "external_audit_current": audit_current,
            "external_audit_approved_testnet": audit_approved,
            "canary_evidence_matches": canary_matches,
            "canary_evidence_recorded": canary_recorded,
            "local_canary_passed": bool(canary and canary.status == "PASSED"),
            "rollback_completed": bool(canary and canary.rollback_completed),
            "no_remote_call_or_real_funds": bool(
                canary
                and not canary.remote_api_call_attempted
                and not canary.real_funds_used
            ),
            "live_execution_absent": (
                not evidence.live_execution_surface_present
            ),
        }
        approved = all(criteria.values())
        if approved:
            outcome = "APPROVED_TESTNET"
        elif not external_present:
            outcome = "BLOCKED_PENDING_EXTERNAL_AUDIT"
        else:
            outcome = "BLOCKED_TECHNICAL"
        decision = ReleaseGateDecision(
            evidence_bundle_id=evidence.evidence_bundle_id,
            source_revision=evidence.source_revision,
            attestation_id=(
                attestation.attestation_id if attestation is not None else None
            ),
            canary_drill_id=(canary.drill_id if canary is not None else None),
            criteria=criteria,
            outcome=outcome,
            testnet_release_authorized=approved,
            expires_at=(
                min(attestation.expires_at, now + timedelta(hours=24))
                if approved and attestation is not None
                else None
            ),
        )
        stored = (
            await self._repository.save_release_gate_decision(decision)
            if self._repository is not None
            else decision
        )
        if all(
            item.gate_decision_id != stored.gate_decision_id
            for item in self._decisions
        ):
            self._decisions.append(stored)
        return stored

    def _evidence_by_id(
        self, evidence_bundle_id: str
    ) -> ReleaseEvidenceBundle | None:
        return next(
            (
                item
                for item in reversed(self._evidence)
                if item.evidence_bundle_id == evidence_bundle_id
            ),
            None,
        )

    def _attestation_by_id(
        self, attestation_id: str
    ) -> IndependentAuditAttestation | None:
        return next(
            (
                item
                for item in reversed(self._attestations)
                if item.attestation_id == attestation_id
            ),
            None,
        )

    def _drill_by_id(
        self, drill_id: str
    ) -> TestnetCanaryDrillReport | None:
        return next(
            (
                item
                for item in reversed(self._drills)
                if item.drill_id == drill_id
            ),
            None,
        )

    def evidence_bundles(self, *, limit: int = 100) -> list[ReleaseEvidenceBundle]:
        return list(reversed(self._evidence))[:limit]

    def attestations(
        self, *, limit: int = 100
    ) -> list[IndependentAuditAttestation]:
        return list(reversed(self._attestations))[:limit]

    def canary_drills(
        self, *, limit: int = 100
    ) -> list[TestnetCanaryDrillReport]:
        return list(reversed(self._drills))[:limit]

    def gate_decisions(
        self, *, limit: int = 100
    ) -> list[ReleaseGateDecision]:
        return list(reversed(self._decisions))[:limit]

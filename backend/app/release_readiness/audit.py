"""Reproducible, read-only repository audit for the Month 12 release gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.schemas.release_readiness import (
    ReleaseAuditCheck,
    ReleaseEvidenceBundle,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ReleaseAuditRunner:
    """Inspect immutable artifacts without importing or controlling the OMS."""

    def __init__(self, repository_root: Path) -> None:
        self.root = repository_root.resolve()

    def collect(
        self,
        *,
        source_revision: str,
        month11_report_id: str,
        month11_report_sha256: str,
        ci_quality_gate_passed: bool,
        database_migrations_validated: bool,
    ) -> ReleaseEvidenceBundle:
        manifest = self._json("packages/contracts/manifest.json")
        contract_paths = manifest.get("schemas", [])
        contracts_complete = (
            len(contract_paths) == 56
            and len(contract_paths) == len(set(contract_paths))
            and all((self.root / "packages/contracts" / path).is_file()
                    for path in contract_paths)
        )
        config = self._text("backend/app/core/config.py")
        oms_schema = self._text("backend/app/schemas/oms.py")
        risk = self._text("backend/app/risk/manager.py")
        context = self._text("backend/app/api/context.py")
        migration = self._text(
            "supabase/migrations/"
            "20260720215457_create_release_readiness_attestations.sql"
        )
        private_append_only = all(
            token in migration.lower()
            for token in (
                "enable row level security",
                "security invoker",
                "revoke all on all tables in schema capital_cipher from public",
                "reject_release_readiness_mutation",
            )
        ) and "grant " not in migration.lower()
        live_absent = (
            'LIVE = "LIVE"' not in oms_schema
            and "ExecutionEnvironment.LIVE" not in context
            and not any(
                "live" in path.name.lower()
                for path in (
                    self.root / "backend/app/execution/adapters"
                ).glob("*.py")
            )
        )
        checks = [
            self._check(
                "ci_quality_gate",
                ci_quality_gate_passed,
                "Official build, typecheck and test gate passed",
                [".github/workflows/ci.yml"],
            ),
            self._check(
                "contracts_versioned",
                contracts_complete,
                "Exactly 56 unique JSON Schema v1 contracts are present",
                ["packages/contracts/manifest.json", *(
                    f"packages/contracts/{path}" for path in contract_paths
                )],
            ),
            self._check(
                "database_migrations_validated",
                database_migrations_validated,
                "All migrations were applied to an empty disposable PostgreSQL schema",
                ["backend/scripts/validate_supabase_migrations.py"],
            ),
            self._check(
                "environment_segregation",
                (
                    'PHASE_1_ALLOWED_MODES: tuple[str, ...] = ("OFFLINE", "PAPER")'
                    in config
                    and 'PAPER = "PAPER"' in oms_schema
                    and 'TESTNET = "TESTNET"' in oms_schema
                    and live_absent
                ),
                "System and OMS environments remain explicitly segregated",
                ["backend/app/core/config.py", "backend/app/schemas/oms.py"],
            ),
            self._check(
                "live_execution_absent",
                live_absent,
                "No LIVE execution enum or adapter is present in runtime source",
                ["backend/app/schemas/oms.py", "backend/app/api/context.py"],
            ),
            self._check(
                "month11_shadow_validation",
                bool(month11_report_id) and len(month11_report_sha256) == 64,
                "A content-addressed Month 11 PAPER validation report was supplied",
                ["docs/month-11-shadow-validation.md"],
            ),
            self._check(
                "private_append_only_evidence",
                private_append_only,
                "Release evidence tables are private, RLS-enabled and immutable",
                [
                    "supabase/migrations/"
                    "20260720215457_create_release_readiness_attestations.sql"
                ],
            ),
            self._check(
                "risk_kill_switch",
                (
                    "async def trigger_kill_switch(" in risk
                    and "ApprovalStatus.REVOKED" in risk
                    and "reset requires MAINTENANCE" in risk
                ),
                "Central kill switch revokes approvals and has a guarded reset",
                ["backend/app/risk/manager.py"],
            ),
            self._check(
                "testnet_explicit_acknowledgement",
                (
                    "TESTNET_ONLY_NO_REAL_FUNDS" in config
                    and "OMS_TESTNET_ENABLED must be explicit" in config
                ),
                "TESTNET requires two explicit server-side acknowledgements",
                ["backend/app/core/config.py"],
            ),
            self._check(
                "testnet_host_allowlist",
                (
                    'value.rstrip("/") != "https://testnet.binance.vision"'
                    in config
                    and 'value.rstrip("/") != "https://api-testnet.bybit.com"'
                    in config
                ),
                "Execution hosts are pinned to exact exchange testnet origins",
                ["backend/app/core/config.py"],
            ),
        ]
        status = "PASSED" if all(item.status == "PASSED" for item in checks) else "FAILED"
        return ReleaseEvidenceBundle(
            source_revision=source_revision,
            source_tree_sha256=self._source_tree_hash(),
            month11_report_id=month11_report_id,
            month11_report_sha256=month11_report_sha256,
            checks=checks,
            status=status,
        )

    def _check(
        self,
        name: str,
        passed: bool,
        summary: str,
        paths,
    ) -> ReleaseAuditCheck:
        digest = hashlib.sha256()
        digest.update(f"{name}:{passed}".encode())
        for relative in sorted(str(path) for path in paths):
            file_path = self.root / relative
            digest.update(relative.replace("\\", "/").encode())
            digest.update(file_path.read_bytes() if file_path.is_file() else b"MISSING")
        return ReleaseAuditCheck(
            check_name=name,
            status="PASSED" if passed else "FAILED",
            summary=summary,
            evidence_sha256=digest.hexdigest(),
        )

    def _source_tree_hash(self) -> str:
        digest = hashlib.sha256()
        roots = ("backend/app", "backend/scripts", "packages/contracts", "supabase/migrations")
        for relative_root in roots:
            root = self.root / relative_root
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                relative = path.relative_to(self.root).as_posix()
                digest.update(relative.encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def _text(self, relative: str) -> str:
        path = self.root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _json(self, relative: str) -> dict:
        text = self._text(relative)
        return json.loads(text) if text else {}

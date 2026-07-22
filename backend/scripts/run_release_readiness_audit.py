"""Print a deterministic Month 12 technical evidence bundle as JSON."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.release_readiness.audit import ReleaseAuditRunner


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--month11-report-id", required=True)
    parser.add_argument("--month11-report-sha256", required=True)
    parser.add_argument("--ci-quality-gate-passed", action="store_true")
    parser.add_argument("--database-migrations-validated", action="store_true")
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[2]
    evidence = ReleaseAuditRunner(repository_root).collect(
        source_revision=args.source_revision,
        month11_report_id=args.month11_report_id,
        month11_report_sha256=args.month11_report_sha256,
        ci_quality_gate_passed=args.ci_quality_gate_passed,
        database_migrations_validated=args.database_migrations_validated,
    )
    print(evidence.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

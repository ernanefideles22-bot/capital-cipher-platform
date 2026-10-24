"""Export a verifiable read-only inventory from the real composition root.

Optional live inputs are saved GET responses, never credentials or SQL URLs.
This command creates no database, starts no workers and performs no network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.context import build_context
from app.core.config import Settings
from app.office.catalog import build_inventory
from app.schemas.agents import AgentHealth, AgentRegistration


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def audit(
    *, registrations: dict | None = None,
    health: dict | None = None,
    status: dict | None = None,
    expected_source_revision: str | None = None,
) -> dict:
    if any(item is not None for item in (registrations, health, status)):
        if any(item is None for item in (registrations, health, status, expected_source_revision)):
            raise ValueError("Live comparison requires registrations, health, status and expected source revision")
        runtime = status["data"]
        if (
            runtime["mode"] != "PAPER"
            or runtime["oms"]["environment"] != "PAPER"
            or runtime["oms"]["live_execution_available"] is not False
            or runtime["source_revision"] != expected_source_revision
        ):
            raise ValueError("Observed runtime is outside the expected PAPER baseline")
    context = build_context(Settings(
        _env_file=None,
        app_env="test", system_mode="PAPER", oms_execution_environment="PAPER",
        oms_testnet_enabled=False, enable_market_data=False, redis_url=None,
        database_url="sqlite+aiosqlite:///:memory:",
    ), with_database=False)
    assert context.agent_registry is not None
    report = build_inventory(
        context.agent_registry,
        observed_registrations=[AgentRegistration.model_validate(row) for row in registrations["data"]["agents"]] if registrations else None,
        observed_health=[AgentHealth.model_validate(row) for row in health["data"]["agents"]] if health else None,
    )
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["runtime_source_revision"] = expected_source_revision if status else None
    report["runtime_observed_at"] = health.get("meta", {}).get("timestamp") if health else None
    report["registered_strategies"] = [item.model_dump(mode="json") for item in context.orchestrator.strategy_engine.list()]
    root = Path(__file__).resolve().parents[2]
    report["audited_source_revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    # Include base lifecycle, indicator implementations and shared control plane,
    # not just the specialist class which references them.
    paths = sorted({
        *[item["implementation"]["path"] for item in report["agents"]],
        "backend/app/agents/base.py", "backend/app/agents/indicators.py",
        "backend/app/agents/regime.py", "backend/app/agents/runtime.py",
        "backend/app/api/context.py", "backend/app/orchestrator/service.py",
        "backend/app/orchestrator/decision_engine.py",
        "backend/app/orchestrator/portfolio_consensus.py",
        "backend/app/strategy/engine.py", "backend/app/risk/manager.py",
        "backend/app/oms/service.py", "backend/app/office/catalog.py",
        "backend/app/office/contracts.py",
    })
    report["source_manifest"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths}
    report["input_sha256"] = {
        name: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for name, value in {"registrations": registrations, "health": health, "status": status}.items()
        if value is not None
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--registrations", type=Path)
    parser.add_argument("--health", type=Path)
    parser.add_argument("--status", type=Path)
    parser.add_argument("--expected-source-revision")
    args = parser.parse_args()
    report = audit(
        registrations=_read(args.registrations) if args.registrations else None,
        health=_read(args.health) if args.health else None,
        status=_read(args.status) if args.status else None,
        expected_source_revision=args.expected_source_revision,
    )
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "agent_count": report["agent_count"], "decision_roles": report["decision_roles"],
        "live_comparison": report["runtime_health_observed"],
        "distribution_compatible": report["requested_distribution_compatible_without_retraining"],
        "output": str(args.output),
    }))


if __name__ == "__main__":
    main()

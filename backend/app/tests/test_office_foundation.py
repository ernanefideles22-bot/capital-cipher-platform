"""Office audit must preserve the 300-agent operational boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.base import BaseAgent
from app.api.context import build_context
from app.core.config import Settings
from app.office.catalog import DEPARTMENTS, build_inventory
from app.office.contracts import Department, OfficeAnalysis


@pytest.fixture
def context():
    return build_context(Settings(_env_file=None, redis_url=None), with_database=False)


def test_inventory_preserves_real_cohort_and_does_not_execute(context, monkeypatch):
    registry = context.agent_registry
    before = [item.model_dump(mode="json") for item in registry.registrations()]
    states = [agent.health() for agent in registry.agents.values()]

    async def forbidden(*args, **kwargs):
        raise AssertionError("Audit must never execute or initialize an agent")

    monkeypatch.setattr(BaseAgent, "run", forbidden)
    monkeypatch.setattr(BaseAgent, "initialize", forbidden)
    report = build_inventory(registry)
    assert report["agent_count"] == 300
    assert report["decision_roles"] == {"PRIMARY": 3, "SHADOW": 297}
    assert sum(item.target for item in DEPARTMENTS) == 300
    assert sum(item["existing_affiliations"] for item in report["departments"]) == 300
    assert len({item["registration"]["agent_name"] for item in report["agents"]}) == 300
    assert report["runtime_health_observed"] is False
    assert report["observed_status_counts"] is None
    assert report["requested_distribution_compatible_without_retraining"] is False
    assert [item.model_dump(mode="json") for item in registry.registrations()] == before
    assert [agent.health() for agent in registry.agents.values()] == states
    for item in report["agents"]:
        assert item["health_observation"] is None
        assert item["latency_ms"]["mean"] is None
        assert item["latency_ms"]["p99"] is None
        assert not any(item["permissions"].values())
        assert item["llm_dependency"] is False
        assert item["live_source_coverage"] is None
    assert report["green_coff"] == {"status": "RESERVED_UNDOCUMENTED", "enabled": False}


def test_provenance_and_existing_external_coverage_are_explicit(context):
    report = build_inventory(context.agent_registry)
    rows = {item["registration"]["agent_name"]: item for item in report["agents"]}
    assert rows["FundingRateAgent"]["collector_implemented"] is True
    assert rows["OptionsSkewAgent"]["collector_implemented"] is False
    assert rows["NewsSentimentAgent"]["collector_implemented"] is False
    assert rows["NewsSentimentAgent"]["proposed_department"] == Department.MACRO
    assert rows["TrendAgent"]["registration"]["decision_role"] == "PRIMARY"
    root = Path(__file__).resolve().parents[3]
    for row in rows.values():
        path = root / row["implementation"]["path"]
        assert path.exists()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["implementation"]["file_sha256"]


def test_live_health_uses_observed_values_without_promoting_ready_to_intelligence(context):
    registry = context.agent_registry
    health = [agent.health() for agent in registry.agents.values()]
    health[0] = health[0].model_copy(update={"status": "TIMEOUT", "total_runs": 2, "avg_latency_ms": 123.5})
    report = build_inventory(registry, observed_registrations=registry.registrations(), observed_health=health)
    assert report["observed_status_counts"] == {"TIMEOUT": 1, "REGISTERED": 299}
    assert report["agents"][0]["latency_ms"]["mean"] == 123.5
    assert report["agents"][0]["latency_ms"]["p95"] is None
    assert not report["agents"][0]["memory"]["adaptive_learning_implemented"]


@pytest.mark.parametrize("kind", ["missing_registration", "duplicate_registration", "changed_hash", "changed_role", "missing_health", "duplicate_health", "changed_version"])
def test_inventory_rejects_live_definition_and_health_drift(context, kind):
    registrations = context.agent_registry.registrations()
    health = [agent.health() for agent in context.agent_registry.agents.values()]
    if kind == "missing_registration":
        registrations.pop()
    elif kind == "duplicate_registration":
        registrations.append(registrations[0])
    elif kind == "changed_hash":
        registrations[0] = registrations[0].model_copy(update={"definition_hash": "f" * 64})
    elif kind == "changed_role":
        registrations[0] = registrations[0].model_copy(update={"decision_role": "SHADOW"})
    elif kind == "missing_health":
        health.pop()
    elif kind == "duplicate_health":
        health.append(health[0])
    else:
        health[0] = health[0].model_copy(update={"version": "9.9.9"})
    with pytest.raises(ValueError):
        build_inventory(context.agent_registry, observed_registrations=registrations, observed_health=health)


def test_unknown_agent_cannot_be_silently_categorized(context, monkeypatch):
    agent = context.agent_registry.get("QuantAgent")
    monkeypatch.setattr(type(agent), "__module__", "unreviewed.implementation")
    with pytest.raises(ValueError, match="Unreviewed"):
        build_inventory(context.agent_registry)


def analysis_payload():
    now = datetime(2026, 10, 24, 8, tzinfo=timezone.utc)
    return dict(
        analysis_id="analysis-1", correlation_id="office-correlation", agent_name="QuantAgent",
        agent_version="1.0.0", source_revision="b" * 40, definition_hash="a" * 64,
        department="ADVANCED_TECHNICAL", symbol="BTCUSDT", timeframe="15m", direction="BUY",
        confirmation=("Closed candle above resistance",), invalidation=("Close below pivot",),
        evidence_ids=("persisted-candle-manifest-1",),
        as_of=now, published_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(seconds=60), max_age_seconds=60,
    )


def test_analysis_validity_is_bound_to_evidence_time_and_round_trips():
    analysis = OfficeAnalysis(**analysis_payload())
    assert not analysis.usable_at(analysis.as_of)
    assert analysis.usable_at(analysis.published_at)
    assert analysis.usable_at(analysis.expires_at - timedelta(microseconds=1))
    assert not analysis.usable_at(analysis.expires_at)
    assert not analysis.usable_at(analysis.expires_at + timedelta(seconds=1))
    assert OfficeAnalysis.model_validate_json(analysis.model_dump_json()) == analysis
    with pytest.raises(ValueError):
        analysis.usable_at(datetime(2026, 10, 24))
    with pytest.raises(ValidationError):
        analysis.direction = "SELL"


@pytest.mark.parametrize("change", [
    {"execution_mode": "LIVE"}, {"execution_mode": "TESTNET"},
    {"order_authority": True}, {"risk_authority": True}, {"promotion_authority": True},
    {"strategy_update": {}}, {"timeframe": "5m"}, {"max_age_seconds": 0},
    {"max_age_seconds": 59}, {"evidence_ids": ()}, {"confirmation": (" ",)},
    {"invalidation": ()}, {"definition_hash": "unknown"},
    {"as_of": datetime(2026, 10, 24, 8)},
    {"published_at": datetime(2026, 10, 24, 8, 2, tzinfo=timezone.utc)},
])
def test_analysis_rejects_authority_escalation_stale_evidence_and_unbounded_inputs(change):
    with pytest.raises(ValidationError):
        OfficeAnalysis(**(analysis_payload() | change))


def _audit_module():
    path = Path(__file__).resolve().parents[2] / "scripts/audit_ai_trading_office.py"
    spec = importlib.util.spec_from_file_location("office_audit_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_audit_offline_ignores_hosted_environment_secrets(monkeypatch):
    monkeypatch.setenv("SYSTEM_MODE", "TESTNET")
    monkeypatch.setenv("OMS_EXECUTION_ENVIRONMENT", "TESTNET")
    monkeypatch.setenv("DATABASE_URL", "postgresql://do-not-connect.invalid/forbidden")
    monkeypatch.setenv("REDIS_URL", "redis://do-not-connect.invalid/0")
    report = _audit_module().audit()
    assert report["agent_count"] == 300
    assert report["runtime_source_revision"] is None
    assert report["runtime_health_observed"] is False
    assert report["registered_strategies"][0]["strategy_id"] == "SCALP_15M"
    assert "backend/app/risk/manager.py" in report["source_manifest"]
    assert "do-not-connect" not in json.dumps(report)


@pytest.mark.parametrize("mode,environment,live,revision", [
    ("LIVE", "PAPER", False, "baseline"), ("PAPER", "TESTNET", False, "baseline"),
    ("PAPER", "PAPER", True, "baseline"), ("PAPER", "PAPER", False, "different"),
])
def test_cli_refuses_unsafe_or_mismatched_runtime(mode, environment, live, revision):
    with pytest.raises(ValueError, match="outside"):
        _audit_module().audit(
            registrations={}, health={}, expected_source_revision="baseline",
            status={"data": {"mode": mode, "source_revision": revision,
                "oms": {"environment": environment, "live_execution_available": live}}},
        )


def test_cli_requires_complete_live_evidence():
    with pytest.raises(ValueError, match="requires"):
        _audit_module().audit(health={})

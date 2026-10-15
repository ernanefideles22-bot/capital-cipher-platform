"""Safety invariants for high-volume PAPER retention."""

from __future__ import annotations

from app.maintenance.paper_high_volume_retention import (
    HIGH_VOLUME_TARGETS,
    _RETENTION_PORTFOLIO_GUARD,
    _RETENTION_SPECIALIST_GUARD,
    _STRICT_PORTFOLIO_GUARD,
    _STRICT_SPECIALIST_GUARD,
)


def test_forecast_outcomes_are_pruned_before_forecasts():
    names = [target.table_name for target in HIGH_VOLUME_TARGETS]
    assert names.index("agent_forecast_outcomes") < names.index("agent_forecasts")
    forecast = next(
        target for target in HIGH_VOLUME_TARGETS if target.table_name == "agent_forecasts"
    )
    assert "NOT EXISTS" in forecast.extra_predicate
    assert "agent_forecast_outcomes" in forecast.extra_predicate


def test_only_immutable_targets_use_guard_relaxation():
    guards = {target.table_name: target.guard for target in HIGH_VOLUME_TARGETS}
    assert guards["agent_forecast_outcomes"] == "specialist"
    assert guards["agent_forecasts"] == "specialist"
    assert guards["drift_observations"] == "portfolio"
    assert guards["agent_outputs"] is None
    assert guards["raw_market_events"] is None


def test_retention_guards_allow_only_authorized_delete():
    for statement in (_RETENTION_SPECIALIST_GUARD, _RETENTION_PORTFOLIO_GUARD):
        assert "TG_OP = 'DELETE'" in statement
        assert "paper_retention_authorized" in statement
        assert "paper-retention-v1" in statement
        assert "RETURN OLD" in statement


def test_strict_guards_never_contain_retention_bypass():
    for statement in (_STRICT_SPECIALIST_GUARD, _STRICT_PORTFOLIO_GUARD):
        assert "paper_retention_authorized" not in statement
        assert "RAISE EXCEPTION" in statement


def test_high_volume_targets_use_bounded_timestamp_columns():
    expected = {
        "agent_forecast_outcomes": "created_at",
        "agent_forecasts": "created_at",
        "drift_observations": "created_at",
        "agent_outputs": "created_at",
        "raw_market_events": "received_at",
    }
    assert {target.table_name: target.timestamp_column for target in HIGH_VOLUME_TARGETS} == expected

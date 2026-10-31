"""E1 PAPER forensics regression tests: diagnostic evidence without false causality."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.paper_forensics_e1 import (
    AgentObservation, PaperTrace, analyze_paper_trades,
)
from scripts.paper_e1_readonly_report import read_only_url

BASE = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def trace(
    id: str = "paper-1", *,
    side="BUY", symbol="BTCUSDT", strategy="SCALP_15M_V1",
    timeframe="15m", entry=100.0, stop=98.0, target=104.0,
    pnl=-10.0, status="CLOSED", opened=BASE,
    closed=None, agents=(), audit=True, decision=True, risk=True,
    exit_reason="STOP_LOSS", exit_price=98.0,
):
    is_closed = status == "CLOSED"
    return PaperTrace(
        order_id=id, decision_id="decision:"+id,
        risk_check_id="risk:"+id, correlation_id="corr:"+id,
        symbol=symbol, timeframe=timeframe, strategy=strategy,
        side=side, status=status,
        entry_price=entry, position_notional=1000,
        opened_at=opened,
        closed_at=(closed or opened + timedelta(hours=1)) if is_closed else None,
        pnl_net=pnl if is_closed else None,
        stop_loss=stop, take_profit=target,
        fees_estimated=2, slippage_estimated=1,
        exit_price=exit_price if audit else None,
        exit_reason=exit_reason if audit else None,
        decision_action=side if decision else None,
        decision_confidence=70 if decision else None,
        decision_trace_present=decision,
        risk_trace_present=risk,
        risk_approved=True if risk else None,
        risk_reward_declared=2.0 if risk else None,
        audit_close_present=audit,
        agents=agents,
    )


def finding(report, id):
    return next(x for x in report.trades if x.order_id == id)


def test_net_pnl_uses_recorded_net_without_fee_slippage_double_subtraction():
    report = analyze_paper_trades([
        trace("loss", pnl=-10), trace("win", pnl=20, exit_reason="TAKE_PROFIT", exit_price=104),
    ])
    assert report.closed_trades == 2
    assert report.net_pnl == 10  # NOT 10 minus recorded fees again
    assert report.win_rate == .5
    assert report.profit_factor == 2.0
    assert {x.key for x in report.by_exit_reason} == {"STOP_LOSS", "TAKE_PROFIT"}
    assert finding(report, "loss").realized_r == -0.5
    assert finding(report, "loss").planned_reward_risk == 2.0
    assert not report.agent_attribution_verified
    assert not report.execution_authorized


def test_no_agent_causal_attribution_even_with_100pct_directional_alignment():
    outputs = (
        AgentObservation("Agent-A", "BUY", BASE - timedelta(minutes=5), confidence=99),
        AgentObservation("Agent-B", "BUY", BASE - timedelta(minutes=4), confidence=95),
    )
    result = analyze_paper_trades([trace(agents=outputs) for _ in []] + [
        trace("a", agents=outputs, pnl=-20),
    ])
    assert result.trades[0].agent_agreement == "AGENTS_ALIGNED_WITH_ENTRY"
    assert result.trades[0].source_attribution == "OBSERVATIONAL_ONLY_NOT_CAUSAL"
    assert not result.agent_attribution_verified
    assert result.by_strategy[0].enough_for_ranking is False


def test_opposing_positions_overlap_on_same_symbol_even_different_timeframes():
    a = trace("a", side="BUY", opened=BASE, closed=BASE + timedelta(hours=3))
    b = trace("b", side="SELL", strategy="DAY_1H_V1", timeframe="1h",
              stop=102, target=96, opened=BASE + timedelta(minutes=30))
    report = analyze_paper_trades([a, b])
    assert report.flags_count["OPPOSING_POSITION_OVERLAP"] == 2
    assert "OPPOSING_POSITION_OVERLAP" in finding(report, "a").flags
    assert "INVALID_STOP_SIDE" not in finding(report, "b").flags
    assert "INVALID_TARGET_SIDE" not in finding(report, "b").flags


def test_different_venues_do_not_create_false_opposing_overlap():
    first = replace(trace("a", side="BUY"), exchange="GATE")
    second = replace(
        trace("b", side="SELL", stop=102, target=96),
        exchange="BINANCE",
    )
    report = analyze_paper_trades([first, second])
    assert "OPPOSING_POSITION_OVERLAP" not in report.flags_count


def test_sequential_opposite_positions_not_false_positives():
    first = trace("a", closed=BASE + timedelta(hours=1))
    next_trade = trace("b", side="SELL", stop=102, target=96,
                       opened=BASE + timedelta(hours=1))
    result = analyze_paper_trades([next_trade, first])
    assert "OPPOSING_POSITION_OVERLAP" not in result.flags_count


def test_incomplete_audit_is_not_silently_treated_as_valid_evidence():
    r = analyze_paper_trades([trace("a", audit=False, decision=False, risk=False)])
    x = r.trades[0]
    assert x.evidence_status == "INCOMPLETE_OR_OPEN"
    assert "AUDIT_CLOSURE_MISSING" in x.flags
    assert "DECISION_TRACE_MISSING" in x.flags
    assert "RISK_TRACE_MISSING" in x.flags
    assert r.missing_audit_closures == 1


def test_planned_stop_target_side_and_low_reward_flags():
    invalid = trace("invalid", stop=101, target=99)
    low = trace("low", stop=90, target=105)
    result = analyze_paper_trades([invalid, low])
    assert "INVALID_STOP_SIDE" in finding(result, "invalid").flags
    assert "INVALID_TARGET_SIDE" in finding(result, "invalid").flags
    assert "PLANNED_REWARD_BELOW_RISK" in finding(result, "low").flags
    assert finding(result, "invalid").realized_r is None


def test_agent_opposition_and_conflict_are_observations_only():
    opposing = AgentObservation("A", "SELL", BASE - timedelta(seconds=1))
    aligned = AgentObservation("B", "BUY", BASE - timedelta(seconds=2))
    result = analyze_paper_trades([
        trace("opposed", agents=(opposing,)),
        trace("mixed", agents=(aligned, opposing)),
    ])
    assert result.flags_count["AGENT_OPPOSED_DIRECTION"] == 1
    assert result.flags_count["AGENT_OPINION_CONFLICT"] == 1
    assert all(t.source_attribution == "OBSERVATIONAL_ONLY_NOT_CAUSAL"
               for t in result.trades)


def test_open_trade_not_marked_as_realized_and_no_unrealized_double_count():
    result = analyze_paper_trades([
        trace("open", status="FILLED"),
        trace("closed", pnl=-50),
    ])
    assert result.open_trades == 1
    assert result.closed_trades == 1
    assert result.net_pnl == -50
    assert finding(result, "open").realized_net is None


def test_take_profit_can_still_have_net_loss_and_requires_investigation():
    t = trace("t", pnl=-5, exit_reason="TAKE_PROFIT", exit_price=104)
    report = analyze_paper_trades([t])
    assert "TAKE_PROFIT_NET_LOSS" in finding(report, "t").flags


def test_duplicate_order_rejected_and_nonfinite_values_rejected():
    t = trace("duplicate")
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_paper_trades([t, t])
    with pytest.raises(ValueError, match="Nonfinite"):
        replace(t, pnl_net=float("nan"))
    with pytest.raises(ValueError, match="precedes open"):
        replace(t, closed_at=BASE - timedelta(seconds=1))


def test_large_profitable_subgroup_never_implies_agent_edge():
    group = [trace(f"t-{i}", pnl=1) for i in range(30)]
    report = analyze_paper_trades(group)
    assert report.by_strategy[0].enough_for_ranking
    assert report.profit_factor is None
    assert report.profit_factor_status == "NO_LOSSES_UNDEFINED"
    assert not report.agent_attribution_verified


def test_no_visible_trades_not_reported_as_proof_of_zero_operations():
    report = analyze_paper_trades([])
    assert report.closed_trades == 0
    assert report.profit_factor is None
    assert report.win_rate is None
    assert "retention/archive" in " ".join(report.limitations)


def test_read_only_export_refuses_sqlite_and_normalizes_asyncpg():
    assert read_only_url("postgresql://db-host/example") == (
        "postgresql+asyncpg://db-host/example"
    )
    with pytest.raises(ValueError, match="PostgreSQL"):
        read_only_url("sqlite:////tmp/e1.db")
    with pytest.raises(ValueError, match="required"):
        read_only_url("")


@pytest.mark.asyncio
async def test_real_exporter_materializes_orm_rows_with_read_only_session(monkeypatch):
    """Guard against Core Connection.scalars() returning the first ID column."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy.sql import Select

    from app.database.models import (
        AgentOutputModel, AuditLogModel, DecisionModel, PaperOrderModel,
        RiskCheckModel,
    )
    from scripts import paper_e1_readonly_report as exporter

    opened = datetime.now(timezone.utc) - timedelta(days=1)
    order = SimpleNamespace(
        id="paper-orm-1", decision_id="decision-orm-1",
        risk_check_id="risk-orm-1", correlation_id="corr-orm-1",
        exchange="BINANCE", symbol="BTCUSDT", timeframe="15m",
        strategy="SCALP_15M_V1", side="BUY", status="CLOSED",
        entry_price=100, position_size=1000, stop_loss=98,
        take_profit=104, fees_estimated=2, slippage_estimated=1,
        opened_at=opened, created_at=opened,
        closed_at=opened + timedelta(hours=1), pnl=-10,
    )
    valid_audit = SimpleNamespace(
        entity_id=order.id, correlation_id=order.correlation_id,
        created_at=order.closed_at + timedelta(seconds=1),
        payload={"exit_reason": "STOP_LOSS", "exit_price": 98},
    )
    newer_unrelated_audit = SimpleNamespace(
        entity_id=order.id, correlation_id="unrelated-correlation",
        created_at=order.closed_at + timedelta(seconds=2),
        payload={"exit_reason": "TAKE_PROFIT", "exit_price": 105},
    )
    records = {
        PaperOrderModel: [order], DecisionModel: [],
        RiskCheckModel: [], AuditLogModel: [newer_unrelated_audit, valid_audit],
        AgentOutputModel: [],
    }
    statements = []
    engine = SimpleNamespace(dispose=AsyncMock())

    class ScalarResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return iter(self.values)

    class FakeORMSession:
        def __init__(self, bind, *, expire_on_commit):
            assert bind is engine
            assert expire_on_commit is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def scalar(self, statement):
            statements.append(statement)
            assert str(statement) == "SHOW transaction_read_only"
            return "on"

        async def execute(self, statement):
            statements.append(statement)
            if not isinstance(statement, Select):
                return ScalarResult([])
            entity = statement.column_descriptions[0]["entity"]
            return ScalarResult(records[entity])

    monkeypatch.setattr(exporter, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(exporter, "AsyncSession", FakeORMSession)
    result = await exporter.export_forensics("postgresql://read-only@db/test")

    assert result["report"]["closed_trades"] == 1
    assert result["report"]["net_pnl"] == -10
    assert result["report"]["missing_audit_closures"] == 0
    assert result["report"]["by_exit_reason"][0]["key"] == "STOP_LOSS"
    assert result["archive_reconciliation"] == "NOT_PERFORMED"
    assert result["agent_history_coverage"] == "UNVERIFIED_MAY_BE_PRUNED"
    assert result["report"]["agent_attribution_verified"] is False
    assert str(statements[0]) == "SET TRANSACTION READ ONLY"
    assert str(statements[1]) == "SHOW transaction_read_only"
    assert str(statements[2]).startswith("SET LOCAL statement_timeout")
    assert all(isinstance(stmt, Select) for stmt in statements[3:])
    engine.dispose.assert_awaited_once()


def test_railway_e1_executor_refuses_nonpaper_and_wrong_db(monkeypatch, capsys):
    from scripts.run_paper_e1_railway_audit import verify_environment

    good = {
        "APP_ENV": "staging",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "SYSTEM_MODE": "PAPER",
        "OMS_EXECUTION_ENVIRONMENT": "PAPER",
        "OMS_TESTNET_ENABLED": "false",
        "DATABASE_URL": "postgresql://reporter:dummy@pg.railway.internal:5432/realpaper",
        "CC_PROBE_DATABASE_URL": "postgresql://reporter:dummy@probe.railway.internal:5432/probe",
        "STAGING_EXPECTED_DATABASE_HOST": "pg.railway.internal",
    }
    assert verify_environment(good) == good["DATABASE_URL"]
    with pytest.raises(RuntimeError, match="E1_NOT_PAPER"):
        verify_environment({**good, "SYSTEM_MODE": "LIVE"})
    with pytest.raises(RuntimeError, match="E1_TESTNET_ENABLED"):
        verify_environment({**good, "OMS_TESTNET_ENABLED": "true"})
    with pytest.raises(RuntimeError, match="E1_DATABASE_HOST_MISMATCH"):
        verify_environment({**good, "DATABASE_URL":
            "postgresql://reporter:dummy@other.railway.internal:5432/realpaper"})
    sanitized = capsys.readouterr().out
    assert "E1_HOST_CONTRACT " in sanitized
    assert "other.railway.internal" in sanitized
    assert "pg.railway.internal" in sanitized
    assert "reporter" not in sanitized
    assert "dummy" not in sanitized
    with pytest.raises(RuntimeError, match="E1_DATABASE_HOST_MISMATCH"):
        verify_environment({**good, "DATABASE_URL":
            "postgresql://sensitive-user:sensitive-pass@external.example.com:5432/test"})
    assert "external.example.com" not in capsys.readouterr().out
    # The historical diagnostic URL may alias the canonical backend DB.
    # Identity is determined by the expected private host, not variable name.
    assert verify_environment({
        **good, "CC_PROBE_DATABASE_URL": good["DATABASE_URL"],
    }) == good["DATABASE_URL"]
    assert "E1_PROBE_ALIAS_MATCHES_CANONICAL_HOST" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="E1_DATABASE_HOST_MISMATCH"):
        verify_environment({
            **good,
            "DATABASE_URL": "postgresql://reporter:dummy@probe.railway.internal:5432/probe",
            "CC_PROBE_DATABASE_URL": "postgresql://reporter:dummy@probe.railway.internal:5432/probe",
        })
    with pytest.raises(RuntimeError, match="E1_WRONG_RAILWAY_ENVIRONMENT"):
        verify_environment({**good, "RAILWAY_ENVIRONMENT_NAME": "production"})


def test_railway_e1_executor_logs_no_database_credentials(monkeypatch, capsys):
    from scripts import run_paper_e1_railway_audit as runner
    import asyncio as _asyncio
    import base64 as _base64
    import gzip as _gzip
    import hashlib as _hashlib
    import json as _json

    async def fake_audit():
        return {
            "report": {
                "mode": "PAPER_READ_ONLY_E1",
                "closed_trades": 1, "open_trades": 0, "net_pnl": -2.0,
                "win_rate": 0.0, "profit_factor": 0.0,
                "missing_audit_closures": 0, "missing_decision_traces": 0,
                "missing_risk_traces": 0, "flags_count": {},
                "by_strategy": [], "by_symbol": [], "by_timeframe": [],
                "by_exit_reason": [],
            },
            "order_sample_truncated": False,
            "agents_sample_truncated": False,
            "archive_reconciliation": "NOT_PERFORMED",
            "agent_history_coverage": "UNVERIFIED_MAY_BE_PRUNED",
        }

    monkeypatch.setattr(runner, "audit_once", fake_audit)
    assert runner.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("E1_AUDIT_SUMMARY ")
    assert lines[-1] == "E1_AUDIT_COMPLETE"
    manifest = _json.loads(next(x for x in lines if x.startswith("E1_AUDIT_MANIFEST ")).split(" ", 1)[1])
    chunks = [x.split(" ", 2)[2] for x in lines if x.startswith("E1_AUDIT_CHUNK ")]
    decoded = _gzip.decompress(_base64.b64decode("".join(chunks)))
    assert _hashlib.sha256(decoded).hexdigest() == manifest["sha256"]
    assert _json.loads(decoded)["report"]["net_pnl"] == -2.0


def test_railway_e1_module_startup_subprocess():
    """Railway working-directory invocation must resolve scripts as a package."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    # Intentionally omit APP_ENV/DB credentials: a guarded, expected failure
    # proves Python module imports resolved without connecting to any DB.
    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_paper_e1_railway_audit"],
        cwd=backend,
        env={"PATH": os.environ.get("PATH", ""), "SYSTEM_MODE": "PAPER"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout.strip() == "E1_AUDIT_BLOCKED E1_NOT_STAGING"
    assert "ModuleNotFoundError" not in result.stderr

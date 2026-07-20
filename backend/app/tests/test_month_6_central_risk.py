"""Month 6 risk invariants retained by the Month 8 100-agent cohort."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.agents.advanced_specialists import (
    AdvancedOHLCVSpecialist,
    DEFINITIONS,
)
from app.api.context import build_context
from app.audit.service import AuditService
from app.core.config import Settings
from app.core.errors import RiskError, SystemStateError, ValidationError
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.models import (
    OrderApprovalModel,
    PaperOrderModel,
    RiskControlEventModel,
    RiskControlStateModel,
    RiskEvaluationModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.store import CandleStore
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import CandidateAction, RiskStatus
from app.schemas.risk import ApprovalStatus, RiskLimits
from app.tests.conftest import make_decision, make_series


async def operational_state_machine() -> SystemStateMachine:
    state_machine = SystemStateMachine()
    await state_machine.transition(
        SystemState.INITIALIZING,
        reason="test",
        actor="test",
    )
    await state_machine.transition(
        SystemState.PAPER,
        reason="test",
        actor="test",
    )
    return state_machine


async def risk_stack(
    *,
    limits: RiskLimits | None = None,
    store: CandleStore | None = None,
    repository=None,
):
    state_machine = await operational_state_machine()
    audit = AuditService(repository=repository)
    manager = RiskManager(
        limits or RiskLimits(),
        state_machine,
        audit,
        repository=repository,
        candle_store=store,
    )
    engine = PaperTradingEngine(
        audit,
        manager,
        repository=repository,
    )
    return state_machine, audit, manager, engine


def test_registry_has_300_paper_agents_with_bounded_authority():
    context = build_context(Settings(), with_database=False)
    registrations = context.agent_registry.registrations()
    assert len(registrations) == 300
    assert sum(item.decision_role == "PRIMARY" for item in registrations) == 3
    assert sum(item.decision_role == "SHADOW" for item in registrations) == 297
    assert {definition.name for definition in DEFINITIONS}.issubset(
        {item.agent_name for item in registrations}
    )
    assert all(item.execution_mode == "PAPER" for item in registrations)
    for agent in context.agent_registry.shadow_agents():
        assert not hasattr(agent, "_risk")
        assert not hasattr(agent, "_engine")
        assert not hasattr(agent, "_repository")
        assert not hasattr(agent, "_credentials")


async def test_risk_request_is_idempotent_and_conflicts_fail_closed():
    _, _, manager, _ = await risk_stack()
    decision = make_decision()
    first = await manager.check(
        decision,
        entry_price=100,
        atr=1,
        idempotency_key="same-risk-request",
    )
    repeated = await manager.check(
        decision,
        entry_price=100,
        atr=1,
        idempotency_key="same-risk-request",
    )
    assert repeated == first
    with pytest.raises(ValidationError, match="different input"):
        await manager.check(
            decision,
            entry_price=101,
            atr=1,
            idempotency_key="same-risk-request",
        )


async def test_forged_or_foreign_approval_cannot_create_order():
    _, _, issuing_manager, _ = await risk_stack()
    decision = make_decision()
    check = await issuing_manager.check(decision, entry_price=100, atr=1)

    _, audit, independent_manager, independent_engine = await risk_stack()
    assert independent_engine._audit is audit
    with pytest.raises(RiskError, match="not issued centrally"):
        await independent_engine.create_order(
            decision,
            check,
            current_price=100,
        )
    assert independent_manager.state.open_positions == 0


async def test_concentration_blocks_second_same_symbol_position():
    limits = RiskLimits(
        max_strategy_exposure_percent=200,
        max_symbol_exposure_percent=200,
        max_symbol_concentration_percent=60,
    )
    _, _, manager, engine = await risk_stack(limits=limits)
    first_decision = make_decision()
    first_check = await manager.check(
        first_decision,
        entry_price=100,
        atr=1,
    )
    await engine.create_order(first_decision, first_check, current_price=100)

    second_decision = make_decision()
    second_decision = second_decision.model_copy(
        update={"strategy": "SECOND_STRATEGY"}
    )
    second = await manager.check(
        second_decision,
        entry_price=100,
        atr=1,
    )
    assert second.risk_status == RiskStatus.BLOCKED
    assert "concentration" in second.reason.lower()


async def test_global_strategy_limit_cannot_be_loosened_by_override():
    limits = RiskLimits(max_strategy_exposure_percent=50)
    _, _, manager, _ = await risk_stack(limits=limits)
    check = await manager.check(
        make_decision(),
        entry_price=100,
        atr=1,
        max_strategy_exposure_percent_override=150,
    )
    assert check.risk_status == RiskStatus.REDUCED
    assert (
        check.effective_limits["max_strategy_exposure_percent"]
        == 50
    )
    assert check.position_size == 5_000


async def test_gross_net_and_symbol_caps_reduce_requested_notional():
    limits = RiskLimits(
        max_gross_exposure_percent=40,
        max_net_exposure_percent=40,
        max_symbol_exposure_percent=40,
        max_strategy_exposure_percent=100,
    )
    _, _, manager, _ = await risk_stack(limits=limits)
    check = await manager.check(
        make_decision(),
        entry_price=100,
        atr=1,
    )
    assert check.risk_status == RiskStatus.REDUCED
    assert check.position_size == 4_000
    assert {
        "GROSS_EXPOSURE_LIMIT",
        "NET_EXPOSURE_LIMIT",
        "SYMBOL_EXPOSURE_LIMIT",
    }.issubset(set(check.warnings))


async def test_var_uses_history_and_conservative_proxy_when_needed():
    store = CandleStore()
    prices = [
        100 * (1 + (0.008 if index % 2 else -0.006)) ** (index + 1)
        for index in range(80)
    ]
    for candle in make_series(prices):
        store.add(candle)
    _, _, historical_manager, _ = await risk_stack(store=store)
    historical = await historical_manager.check(
        make_decision(),
        entry_price=prices[-1],
        atr=1,
    )
    assert historical.var_result is not None
    assert historical.var_result.method == "historical-v1"
    assert historical.var_result.observations >= 30

    _, _, proxy_manager, _ = await risk_stack()
    proxy = await proxy_manager.check(
        make_decision(),
        entry_price=100,
        atr=1,
    )
    assert proxy.var_result is not None
    assert proxy.var_result.method == "proxy-v1"
    assert "VAR_PROXY_USED" in proxy.warnings


async def test_approval_expiry_and_single_use_are_enforced():
    _, _, manager, engine = await risk_stack()
    decision = make_decision()
    check = await manager.check(decision, entry_price=100, atr=1)
    approval = manager._approvals[check.approval_id]
    manager._approvals[check.approval_id] = approval.model_copy(
        update={
            "created_at": approval.created_at - timedelta(seconds=120),
            "expires_at": approval.expires_at - timedelta(seconds=120),
        }
    )
    with pytest.raises(RiskError, match="expired"):
        await engine.create_order(decision, check, current_price=100)

    second_decision = make_decision()
    second_check = await manager.check(
        second_decision,
        entry_price=100,
        atr=1,
    )
    await engine.create_order(
        second_decision,
        second_check,
        current_price=100,
    )
    with pytest.raises((RiskError, ValidationError)):
        await engine.create_order(
            second_decision,
            second_check,
            current_price=100,
        )


async def test_approval_becomes_stale_when_portfolio_changes():
    limits = RiskLimits(
        max_gross_exposure_percent=300,
        max_symbol_exposure_percent=200,
        max_strategy_exposure_percent=200,
    )
    _, _, manager, engine = await risk_stack(limits=limits)
    first_decision = make_decision()
    stale_decision = make_decision(action=CandidateAction.SELL)
    stale_decision = stale_decision.model_copy(
        update={"symbol": "ETHUSDT", "strategy": "SECOND_STRATEGY"}
    )
    first_check = await manager.check(
        first_decision,
        entry_price=100,
        atr=1,
    )
    stale_check = await manager.check(
        stale_decision,
        entry_price=100,
        atr=1,
    )
    await engine.create_order(
        first_decision,
        first_check,
        current_price=100,
    )
    with pytest.raises(RiskError, match="stale"):
        await engine.create_order(
            stale_decision,
            stale_check,
            current_price=100,
        )


async def test_kill_switch_revokes_approvals_and_reset_requires_maintenance():
    state_machine, _, manager, engine = await risk_stack()
    decision = make_decision()
    check = await manager.check(decision, entry_price=100, atr=1)
    await manager.trigger_kill_switch(reason="test emergency", actor="test")
    assert state_machine.state == SystemState.ERROR
    assert manager.control_state.active
    assert (
        manager._approvals[check.approval_id].status
        == ApprovalStatus.REVOKED
    )
    with pytest.raises(RiskError):
        await engine.create_order(decision, check, current_price=100)
    with pytest.raises(SystemStateError, match="MAINTENANCE"):
        await manager.reset_kill_switch(reason="too early", actor="test")
    await state_machine.transition(
        SystemState.MAINTENANCE,
        reason="controlled recovery",
        actor="test",
    )
    await manager.reset_kill_switch(reason="verified", actor="test")
    assert not manager.control_state.active
    assert not state_machine.kill_switch_active


async def test_sqlite_central_evidence_and_order_consumption_are_atomic(
    tmp_path,
):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'central-risk.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    _, _, manager, engine = await risk_stack(repository=repository)
    decision = make_decision()
    check = await manager.check(decision, entry_price=100, atr=1)
    order = await engine.create_order(decision, check, current_price=100)

    async with database.session() as session:
        evaluation = await session.get(
            RiskEvaluationModel,
            check.evaluation_id,
        )
        approval = await session.get(
            OrderApprovalModel,
            check.approval_id,
        )
        stored_order = await session.get(PaperOrderModel, order.paper_order_id)
        assert evaluation is not None
        assert approval.status == ApprovalStatus.CONSUMED.value
        assert approval.paper_order_id == order.paper_order_id
        assert stored_order.approval_id == check.approval_id

    restart_state = await operational_state_machine()
    restart_audit = AuditService(repository=repository)
    restarted = RiskManager(
        RiskLimits(),
        restart_state,
        restart_audit,
        repository=repository,
    )
    await restarted.initialize()
    restored_check = await restarted.check(
        decision,
        entry_price=100,
        atr=1,
    )
    assert restored_check == check
    restarted_engine = PaperTradingEngine(
        restart_audit,
        restarted,
        repository=repository,
    )
    with pytest.raises(RiskError, match="CONSUMED"):
        await restarted_engine.create_order(
            decision,
            restored_check,
            current_price=100,
        )
    await database.dispose()


async def test_durable_kill_switch_is_restored_on_restart(tmp_path):
    database = Database(
        f"sqlite+aiosqlite:///{tmp_path / 'risk-control.db'}"
    )
    await database.create_all()
    repository = Repository(database)
    state_machine, _, manager, _ = await risk_stack(repository=repository)
    await manager.trigger_kill_switch(reason="durable emergency", actor="test")
    assert state_machine.state == SystemState.ERROR

    restored_state = SystemStateMachine()
    restored = RiskManager(
        RiskLimits(),
        restored_state,
        AuditService(repository=repository),
        repository=repository,
    )
    await restored.initialize()
    assert restored.control_state.active
    assert restored_state.kill_switch_active
    async with database.session() as session:
        control = await session.get(RiskControlStateModel, 1)
        events = list(
            await session.scalars(select(RiskControlEventModel))
        )
        assert control.active
        assert len(events) == 1
    await database.dispose()


def test_supabase_migration_is_private_fail_safe_and_versioned():
    from pathlib import Path

    root = Path(__file__).parents[3]
    migrations = sorted(
        (root / "supabase" / "migrations").glob(
            "*_create_central_risk_engine.sql"
        )
    )
    assert len(migrations) == 1
    sql = migrations[0].read_text(encoding="utf-8").lower()
    assert "capital_cipher.risk_evaluations" in sql
    assert "capital_cipher.order_approvals" in sql
    assert "capital_cipher.risk_control_state" in sql
    assert "enable row level security" in sql
    assert "security invoker" in sql
    assert "from public" in sql
    assert "append-only" in sql

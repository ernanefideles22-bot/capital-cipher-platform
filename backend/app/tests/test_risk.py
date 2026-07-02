"""Risk Management tests — mandatory scenarios from docs/22."""

from __future__ import annotations

from app.schemas.common import CandidateAction, RiskStatus
from app.tests.conftest import make_decision


async def test_operation_approved(risk_manager):
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.APPROVED
    assert check.approved is True
    assert check.position_size and check.position_size > 0
    assert check.stop_loss < 100.0 < check.take_profit
    assert check.risk_reward >= risk_manager.limits.min_risk_reward


async def test_operation_reduced_near_position_limit(risk_manager):
    risk_manager.set_open_positions(risk_manager.limits.max_open_positions - 1)
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.REDUCED
    assert check.approved is True
    assert check.risk_percent == risk_manager.limits.risk_per_trade_percent * 0.5


async def test_operation_blocked_at_position_limit(risk_manager):
    risk_manager.set_open_positions(risk_manager.limits.max_open_positions)
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED
    assert check.approved is False


async def test_kill_switch_dominates(risk_manager, paper_state_machine):
    await paper_state_machine.trigger_kill_switch(reason="emergency", actor="test")
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.KILL_SWITCH
    assert check.approved is False


async def test_daily_drawdown_exceeded_blocks(risk_manager):
    risk_manager.state.daily_pnl_percent = -5.0
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED
    assert "drawdown" in check.reason.lower()


async def test_consecutive_losses_exceeded_blocks(risk_manager):
    risk_manager.state.consecutive_losses = 3
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED


async def test_bad_data_quality_blocks(risk_manager):
    check = await risk_manager.check(
        make_decision(), entry_price=100.0, atr=1.0, data_quality_score=50
    )
    assert check.risk_status == RiskStatus.BLOCKED


async def test_excessive_latency_blocks(risk_manager):
    check = await risk_manager.check(
        make_decision(), entry_price=100.0, atr=1.0, market_data_delay_ms=10_000
    )
    assert check.risk_status == RiskStatus.BLOCKED


async def test_audit_failure_blocks(risk_manager, audit_service):
    audit_service.fail_mode = True
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED
    assert "audit" in check.reason.lower()


async def test_non_actionable_decision_blocked(risk_manager):
    check = await risk_manager.check(
        make_decision(action=CandidateAction.HOLD), entry_price=100.0, atr=1.0
    )
    assert check.risk_status == RiskStatus.BLOCKED


async def test_offline_system_blocks(state_machine, audit_service):
    from app.risk.manager import RiskManager
    from app.schemas.risk import RiskLimits

    manager = RiskManager(RiskLimits(), state_machine, audit_service)
    check = await manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED

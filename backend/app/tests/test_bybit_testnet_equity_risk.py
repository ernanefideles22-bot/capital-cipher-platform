"""Venue-equity safety tests for Bybit TESTNET risk accounting."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.audit.service import AuditService
from app.core.errors import SecurityError
from app.core.state_machine import SystemState, SystemStateMachine
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.common import CandidateAction
from app.schemas.decisions import Decision
from app.schemas.oms import ExecutionEnvironment
from app.schemas.risk import RiskLimits


async def _operating_state_machine() -> SystemStateMachine:
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


def _decision() -> Decision:
    return Decision(
        correlation_id=str(uuid4()),
        symbol="BTCUSDT",
        timeframe="15m",
        candidate_action=CandidateAction.BUY,
        confidence=90,
        strategy="SCALP_15M",
        agent_summary=[{"name": "QuantAgent", "signal": "BUY"}],
    )


@pytest.mark.asyncio
async def test_testnet_risk_requires_venue_equity_provider():
    manager = VenueAwareRiskManager(
        RiskLimits(),
        await _operating_state_machine(),
        AuditService(),
        execution_environment=ExecutionEnvironment.TESTNET,
        initial_balance=10_000.0,
    )

    with pytest.raises(SecurityError, match="no venue equity provider"):
        await manager.effective_balance(10_000.0)


@pytest.mark.asyncio
async def test_testnet_risk_uses_bybit_equity_instead_of_paper_balance():
    manager = VenueAwareRiskManager(
        RiskLimits(),
        await _operating_state_machine(),
        AuditService(),
        execution_environment=ExecutionEnvironment.TESTNET,
        initial_balance=10_000.0,
    )

    async def equity_provider() -> float:
        return 20_000.0

    manager.set_testnet_equity_provider(equity_provider)
    check = await manager.check(
        _decision(),
        entry_price=100_000.0,
        atr=1_000.0,
        balance=10_000.0,
        idempotency_key="venue-equity-risk-test",
    )

    assert check.portfolio_snapshot is not None
    assert check.portfolio_snapshot.balance == pytest.approx(20_000.0)
    assert manager.initial_balance == pytest.approx(20_000.0)


@pytest.mark.asyncio
async def test_testnet_equity_updates_daily_and_total_drawdown():
    values = iter((20_000.0, 19_000.0))

    async def equity_provider() -> float:
        return next(values)

    manager = VenueAwareRiskManager(
        RiskLimits(),
        await _operating_state_machine(),
        AuditService(),
        execution_environment=ExecutionEnvironment.TESTNET,
        equity_cache_seconds=0.0,
        initial_balance=10_000.0,
    )
    manager.set_testnet_equity_provider(equity_provider)

    assert await manager.effective_balance(10_000.0, force=True) == 20_000.0
    assert await manager.effective_balance(10_000.0, force=True) == 19_000.0
    assert manager.state.daily_pnl_percent == pytest.approx(-5.0)
    assert manager.state.total_drawdown_percent == pytest.approx(5.0)

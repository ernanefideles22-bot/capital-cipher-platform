"""Venue-equity safety tests for Bybit TESTNET risk accounting."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.audit.service import AuditService
from app.core.errors import SecurityError
from app.core.state_machine import SystemState, SystemStateMachine
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.common import CandidateAction, OrderSide
from app.schemas.decisions import Decision
from app.schemas.oms import ExecutionEnvironment
from app.schemas.risk import PositionExposure, RiskLimits


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


def _exposure(identity: str) -> PositionExposure:
    return PositionExposure(
        paper_order_id=identity,
        symbol="BTCUSDT",
        timeframe="15m",
        strategy="TEST",
        side=OrderSide.BUY,
        notional=100.0,
        leverage=1.0,
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


@pytest.mark.asyncio
async def test_testnet_risk_filters_paper_and_other_exchange_exposure():
    managed_id = "11111111-1111-1111-1111-111111111111"
    other_id = "22222222-2222-2222-2222-222222222222"

    class _Repository:
        async def load_open_position_exposures(self):
            return [
                _exposure("paper-order-1"),
                _exposure("venue:BYBIT:TESTNET:BTCUSDT:BUY"),
                _exposure("venue:BINANCE:TESTNET:BTCUSDT:BUY"),
                _exposure(f"oms-reservation:{managed_id}"),
                _exposure(f"oms-reservation:{other_id}"),
                _exposure(f"oms:{managed_id}"),
            ]

        async def list_oms_orders(self, *, exchange, environment, limit):
            assert exchange.value == "BYBIT"
            assert environment == ExecutionEnvironment.TESTNET
            assert limit is None
            return [SimpleNamespace(oms_order_id=managed_id)]

    manager = VenueAwareRiskManager(
        RiskLimits(),
        await _operating_state_machine(),
        AuditService(),
        execution_environment=ExecutionEnvironment.TESTNET,
        initial_balance=10_000.0,
        repository=_Repository(),
    )

    async def equity_provider() -> float:
        return 20_000.0

    manager.set_testnet_equity_provider(equity_provider)
    await manager.refresh_positions()

    identities = {
        item.paper_order_id for item in manager.position_exposures()
    }
    assert identities == {
        "venue:BYBIT:TESTNET:BTCUSDT:BUY",
        f"oms-reservation:{managed_id}",
        f"oms:{managed_id}",
    }


@pytest.mark.asyncio
async def test_paper_history_cannot_reset_testnet_equity_baseline():
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
    await manager.effective_balance(10_000.0, force=True)
    manager.restore_realized_history([], 10_000.0)
    manager.register_trade_result(-1_000.0)

    assert manager.initial_balance == pytest.approx(20_000.0)
    assert manager.state.daily_pnl_percent == pytest.approx(0.0)

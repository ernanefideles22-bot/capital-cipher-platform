"""Gate TESTNET risk must not inherit positions from another venue."""
from types import SimpleNamespace

import pytest

from app.audit.service import AuditService
from app.core.errors import SecurityError
from app.core.state_machine import SystemState, SystemStateMachine
from app.risk.venue_aware import VenueAwareRiskManager
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment
from app.schemas.risk import PositionExposure, RiskLimits


def exposure(identity):
    return PositionExposure(paper_order_id=identity, symbol="BTCUSDT", timeframe="15m",
                            strategy="TEST", side=OrderSide.BUY, notional=100, leverage=1)


@pytest.mark.asyncio
async def test_gate_risk_scopes_positions_and_reservations():
    class Repository:
        async def load_open_position_exposures(self):
            return [exposure("venue:GATE:TESTNET:BTCUSDT:BUY"),
                    exposure("venue:BYBIT:TESTNET:BTCUSDT:BUY"),
                    exposure("venue:GATE:PAPER:BTCUSDT:BUY"),
                    exposure("oms-reservation:gate-id"), exposure("oms-reservation:other-id"),
                    exposure("oms:gate-id"), exposure("oms:other-id"), exposure("paper-id")]

        async def list_oms_orders(self, *, exchange, environment, limit):
            assert exchange is Exchange.GATE
            assert environment is ExecutionEnvironment.TESTNET and limit is None
            return [SimpleNamespace(oms_order_id="gate-id")]

    sm=SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING,reason="test",actor="test")
    await sm.transition(SystemState.PAPER,reason="test",actor="test")
    manager=VenueAwareRiskManager(RiskLimits(),sm,AuditService(),repository=Repository(),
        execution_environment=ExecutionEnvironment.TESTNET,testnet_exchange=Exchange.GATE)
    async def equity():
        return 1000.0
    manager.set_testnet_equity_provider(equity)
    await manager.refresh_positions()
    assert {row.paper_order_id for row in manager.position_exposures()} == {
        "venue:GATE:TESTNET:BTCUSDT:BUY", "oms-reservation:gate-id", "oms:gate-id"}


@pytest.mark.asyncio
async def test_unsupported_exchange_is_rejected():
    with pytest.raises(SecurityError):
        VenueAwareRiskManager(RiskLimits(),SystemStateMachine(),AuditService(),
            execution_environment=ExecutionEnvironment.TESTNET,testnet_exchange=Exchange.BINANCE)


@pytest.mark.asyncio
async def test_gate_without_repository_fails_closed():
    with pytest.raises(SecurityError, match="durable repository"):
        VenueAwareRiskManager(RiskLimits(),SystemStateMachine(),AuditService(),
            execution_environment=ExecutionEnvironment.TESTNET,testnet_exchange=Exchange.GATE)


def test_gate_cannot_fall_through_to_bybit_adapter(monkeypatch):
    from app.api.context import build_context
    from app.core.config import Settings
    from app.execution.credentials import EnvironmentTestnetCredentialProvider

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No credentials should be read for an unsupported adapter")
    monkeypatch.setattr(EnvironmentTestnetCredentialProvider, "load", forbidden)
    settings=Settings(_env_file=None, APP_ENV="test", DATABASE_URL="postgresql+asyncpg://x:x@localhost/x",
        OMS_EXECUTION_ENVIRONMENT="TESTNET", OMS_TESTNET_ENABLED=True,
        OMS_TESTNET_ACKNOWLEDGEMENT="TESTNET_ONLY_NO_REAL_FUNDS").model_copy(
            update={"oms_testnet_exchange":"GATE"})
    with pytest.raises(ValueError, match="no supported execution adapter"):
        build_context(settings, with_database=True)

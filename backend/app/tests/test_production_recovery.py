"""Recovery and authorization regressions identified during deployment review."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.errors import SecurityError
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.release_readiness.service import ReleaseReadinessService
from app.schemas.common import utcnow
from app.schemas.oms import OMSOrderStatus
from app.tests.conftest import make_candle, make_decision
from app.tests.test_month_6_central_risk import risk_stack
from app.tests.test_month_7_oms import oms_stack


async def test_durable_restart_keeps_monitoring_stop_and_reconstructs_equity(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}"
    database = Database(url)
    await database.create_all()
    repository = Repository(database)
    _, _, risk, engine = await risk_stack(repository=repository)
    await risk.initialize()
    decision = make_decision()
    check = await risk.check(decision, entry_price=100, atr=1)
    order = await engine.create_order(decision, check, current_price=100)
    await database.dispose()

    database = Database(url)
    try:
        repository = Repository(database)
        _, _, risk, restored = await risk_stack(repository=repository)
        await risk.initialize()
        await restored.initialize()
        assert risk.state.open_positions == 1
        assert order.paper_order_id in restored.open_orders
        candle = make_candle(80, open_=80, high=81, low=79)
        closed = await restored.on_candle(candle)
        assert len(closed) == 1
        assert closed[0].pnl < 0
        assert risk.state.open_positions == 0
        expected_balance = restored.balance
        expected_daily = risk.state.daily_pnl_percent
        _, _, next_risk, next_engine = await risk_stack(repository=repository)
        await next_risk.initialize()
        await next_engine.initialize()
        assert not next_engine.open_orders
        assert next_engine.balance == expected_balance
        assert next_risk.state.daily_pnl_percent == expected_daily
        assert next_risk.state.consecutive_losses == 1
    finally:
        await database.dispose()


async def test_testnet_requires_guard_even_with_valid_central_approval(tmp_path):
    database, _, _, _, _, oms, adapter, decision, check = await oms_stack(tmp_path)
    try:
        oms._release_guard = None
        with pytest.raises(SecurityError, match="release is not authorized"):
            await oms.submit_approved(decision, check, current_price=100)
        assert adapter.submit_calls == 0
        assert not await oms.dispatch_once()
    finally:
        await database.dispose()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_release_is_rechecked_before_queued_write(tmp_path, unavailable):
    database, _, _, _, risk, oms, adapter, decision, check = await oms_stack(tmp_path)
    try:
        order = await oms.submit_approved(decision, check, current_price=100)

        async def revoked():
            if unavailable:
                raise ConnectionError("database unavailable")
            return False

        oms._release_guard = revoked
        assert await oms.dispatch_once()
        assert adapter.submit_calls == 0
        assert risk.kill_switch_active
        assert (await oms.get_order(order.oms_order_id)).status == OMSOrderStatus.QUARANTINED
    finally:
        await database.dispose()


async def test_runtime_gate_uses_fresh_latest_revision_and_expiry():
    now = utcnow()
    approved = SimpleNamespace(
        source_revision="a" * 40,
        outcome="APPROVED_TESTNET",
        testnet_release_authorized=True,
        decided_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=1),
    )

    class GateRepository:
        decisions = []

        async def list_release_gate_decisions(self, *, limit):
            assert limit == 1
            return self.decisions[:limit]

    repository = GateRepository()
    service = ReleaseReadinessService(repository)
    assert not await service.runtime_authorized("a" * 40)
    repository.decisions = [approved]
    assert await service.runtime_authorized("a" * 40)
    assert not await service.runtime_authorized("b" * 40)
    assert not await service.runtime_authorized("")
    approved.expires_at = now - timedelta(seconds=1)
    assert not await service.runtime_authorized("a" * 40)
    approved.expires_at = now + timedelta(minutes=1)
    approved.outcome = "BLOCKED_TECHNICAL"
    assert not await service.runtime_authorized("a" * 40)
    assert not await ReleaseReadinessService().runtime_authorized("a" * 40)


async def test_expired_release_does_not_prevent_cancel(tmp_path):
    database, _, _, _, _, oms, adapter, decision, check = await oms_stack(tmp_path)
    try:
        order = await oms.submit_approved(decision, check, current_price=100)
        assert await oms.dispatch_once()
        oms._release_guard = None
        await oms.queue_cancel(order.oms_order_id)
        assert await oms.dispatch_once()
        assert adapter.cancel_calls == 1
        assert adapter.submit_calls == 1
    finally:
        await database.dispose()

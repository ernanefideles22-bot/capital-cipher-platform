from __future__ import annotations

from app.audit.service import AuditService
from app.core.state_machine import SystemState, SystemStateMachine
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.local_bybit_emulator import LocalBybitTestnetEmulator
from app.oms.bybit_testnet_exit import BybitTestnetExitService
from app.oms.reconciliation import ReconciliationService
from app.oms.service import OMSService
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import CandidateAction, Exchange
from app.schemas.oms import ExecutionEnvironment, OMSOrderStatus, ReconciliationRunStatus
from app.schemas.risk import RiskLimits
from app.tests.conftest import make_decision


async def _allow_local_test_release() -> bool:
    return True


async def test_local_bybit_emulator_end_to_end_canary(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'local-bybit-canary.db'}")
    await database.create_all()
    repository = Repository(database)

    state_machine = SystemStateMachine()
    await state_machine.transition(
        SystemState.INITIALIZING,
        reason="local emulator canary",
        actor="test",
    )
    await state_machine.transition(
        SystemState.PAPER,
        reason="local emulator canary",
        actor="test",
    )

    audit = AuditService(repository=repository)
    risk = RiskManager(
        RiskLimits(),
        state_machine,
        audit,
        repository=repository,
    )
    await risk.initialize()
    paper = PaperTradingEngine(audit, risk, repository=repository)
    adapter = LocalBybitTestnetEmulator(
        initial_equity=10_000.0,
        default_fill_fraction=1.0,
    )

    oms = OMSService(
        target_environment=ExecutionEnvironment.TESTNET,
        target_exchange=Exchange.BYBIT,
        paper_engine=paper,
        risk_manager=risk,
        audit_service=audit,
        adapters={(Exchange.BYBIT, ExecutionEnvironment.TESTNET): adapter},
        repository=repository,
        release_guard=_allow_local_test_release,
    )

    decision = make_decision(CandidateAction.BUY)
    check = await risk.check(decision, entry_price=100.0, atr=1.0)
    pending = await oms.submit_approved(
        decision,
        check,
        current_price=100.0,
    )
    assert pending.status == OMSOrderStatus.PENDING_SUBMISSION

    assert await oms.dispatch_once() is True
    filled = await oms.get_order(pending.oms_order_id)
    assert filled is not None
    assert filled.status == OMSOrderStatus.FILLED
    assert filled.cumulative_filled_quantity > 0

    venue_after_entry = await adapter.fetch_state(symbols={decision.symbol})
    assert len(venue_after_entry.positions) == 1
    assert venue_after_entry.positions[0].quantity == filled.cumulative_filled_quantity
    assert venue_after_entry.balances[0].asset == "USDT"
    assert venue_after_entry.balances[0].equity == 10_000.0

    reconciliation = ReconciliationService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
        repository=repository,
    )
    run = await reconciliation.reconcile_once()
    assert run.status == ReconciliationRunStatus.MATCHED
    assert run.critical_mismatch_count == 0

    await risk.trigger_kill_switch(
        reason="local emulator canary controlled flatten",
        actor="test",
        correlation_id=decision.correlation_id,
    )
    assert risk.kill_switch_active is True

    exit_service = BybitTestnetExitService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
    )
    flattened = await exit_service.flatten_after_kill_switch(
        correlation_id=decision.correlation_id,
        settle_timeout_seconds=1.0,
        poll_interval_seconds=0.01,
    )
    assert flattened.requested_positions == 1
    assert flattened.acknowledged_exits == 1
    assert flattened.reconciliation_flat is True

    final_state = await adapter.fetch_state(symbols={decision.symbol})
    assert final_state.positions == []
    assert final_state.balances[0].equity == 10_000.0

    await database.dispose()

"""Audit regressions: mixed-service OMS storage and immutable PAPER reporting."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.api.routes.paper import get_order as paper_get_order, list_orders as paper_list_orders
from app.api.routes.status import system_status
from app.core.config import Settings
from app.core.errors import ValidationError
from app.database.models import ExecutionCommandModel, OMSOrderModel
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.paper import PaperExecutionAdapter
from app.oms.service import OMSService
from app.paper_trading.engine import PaperTradingEngine
from app.paper_trading.execution import RealisticExecutionModel
from app.schemas.backtest import BacktestExecutionAssumptions
from app.schemas.common import CandidateAction, Exchange
from app.schemas.oms import ExecutionCommand, ExecutionEnvironment
from app.strategy.engine import StrategyEngine
from app.tests.conftest import make_candle, make_decision
from app.tests.test_month_7_oms import sample_oms_order


@pytest.fixture
async def mixed_oms(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mixed.db'}")
    await db.create_all()
    repo = Repository(db)
    orders = []
    commands = []
    now = datetime.now(timezone.utc)
    async with db.session() as session, session.begin():
        # Disposable SQLite only: simulate the shared schema expanded by the
        # Gate service while this backend still has the older Exchange enum.
        await session.execute(text("PRAGMA ignore_check_constraints=ON"))
        for index, (exchange, environment, expired) in enumerate([
            ("BINANCE", "PAPER", False),
            ("BINANCE", "TESTNET", False),
            ("BYBIT", "TESTNET", False),
            ("GATE", "TESTNET", False),
            ("GATE", "TESTNET", True),
        ]):
            order = sample_oms_order().model_copy(update={
                "oms_order_id": str(uuid4()), "client_order_id": str(uuid4()),
                "approval_id": uuid4().hex + uuid4().hex,
                "environment": ExecutionEnvironment(environment),
                "created_at": now + timedelta(seconds=index),
            })
            values = repo._oms_order_values(order)
            values["exchange"] = exchange
            session.add(OMSOrderModel(**values))
            orders.append(order)
            command = ExecutionCommand(oms_order_id=order.oms_order_id, command_type="SUBMIT")
            values = repo._execution_command_values(command)
            if expired:
                values.update(status="LEASED", attempt_count=1, leased_by="gate-worker",
                              lease_expires_at=now - timedelta(seconds=1))
            session.add(ExecutionCommandModel(**values))
            commands.append(command)
        await session.flush()
        await session.execute(text("PRAGMA ignore_check_constraints=OFF"))
    try:
        yield db, repo, orders, commands
    finally:
        await db.dispose()


async def test_scope_filters_before_mapping_and_limit(mixed_oms, paper_engine, risk_manager, audit_service):
    db, repo, orders, commands = mixed_oms
    service = OMSService(
        target_environment=ExecutionEnvironment.PAPER, target_exchange=Exchange.BINANCE,
        paper_engine=paper_engine, risk_manager=risk_manager, audit_service=audit_service,
        adapters={(Exchange.BINANCE, ExecutionEnvironment.PAPER): PaperExecutionAdapter(paper_engine)},
        repository=repo,
    )
    await service.initialize()
    assert list(service._orders) == [orders[0].oms_order_id]
    assert [o.oms_order_id for o in await service.list_orders(limit=1)] == [orders[0].oms_order_id]
    assert (await service.get_order(orders[0].oms_order_id)).oms_order_id == orders[0].oms_order_id
    for foreign in orders[1:]:
        assert await service.get_order(foreign.oms_order_id) is None
        with pytest.raises(ValidationError, match="does not exist"):
            await service.queue_cancel(foreign.oms_order_id)
    assert not await service.dispatch_once()
    async with db.session() as session:
        rows = list(await session.scalars(select(ExecutionCommandModel)))
        assert len(rows) == len(commands)
        assert sorted(row.status for row in rows) == ["LEASED", "PENDING", "PENDING", "PENDING", "PENDING"]
        assert sum(row.attempt_count for row in rows) == 1


async def test_scoped_claim_does_not_lease_or_recover_foreign_commands(mixed_oms):
    db, repo, orders, commands = mixed_oms
    scope = dict(exchange=Exchange.BINANCE, environment=ExecutionEnvironment.TESTNET)
    claimed = await repo.claim_execution_command(worker_id="binance", lease_seconds=10, **scope)
    assert claimed[0].command_id == commands[1].command_id
    assert claimed[1].oms_order_id == orders[1].oms_order_id
    assert await repo.claim_execution_command(worker_id="binance", lease_seconds=10, **scope) is None
    async with db.session() as session:
        for index in (0, 2, 3):
            row = await session.get(ExecutionCommandModel, commands[index].command_id)
            assert (row.status, row.attempt_count) == ("PENDING", 0)
        expired = await session.get(ExecutionCommandModel, commands[4].command_id)
        assert (expired.status, expired.leased_by, expired.attempt_count) == ("LEASED", "gate-worker", 1)
        assert expired.last_error_type is None
        foreign = await session.get(OMSOrderModel, orders[4].oms_order_id)
        assert foreign.status == "PENDING_SUBMISSION"
    assert await repo.load_oms_order(orders[3].oms_order_id, **scope) is None


@pytest.mark.parametrize("action", [CandidateAction.BUY, CandidateAction.SELL])
async def test_basic_slippage_is_cash_without_changing_trade_or_double_charging(
    action, paper_engine, risk_manager, audit_service,
):
    decision = make_decision(action=action)
    check = await risk_manager.check(decision, entry_price=100, atr=1)
    opened = await paper_engine.create_order(decision, check, current_price=100)
    closed = await paper_engine.close_order(opened.paper_order_id, 100, "TEST_END")
    snapshot = closed.model_dump(mode="json")
    quantity = closed.position_size / closed.entry_price
    expected_slippage = 0.02 * quantity
    sign = 1 if action == CandidateAction.BUY else -1
    expected_gross = sign * (100 - closed.entry_price) * quantity
    assert closed.slippage_estimated == 0.02
    assert closed.pnl == pytest.approx(round(expected_gross - closed.fees_estimated, 4))
    performance = paper_engine.performance()
    assert performance.slippage_total == round(expected_slippage, 4)
    assert performance.gross_pnl == pytest.approx(expected_gross, abs=0.0001)
    assert performance.net_pnl == closed.pnl
    assert performance.total_execution_cost == round(performance.fees_total + round(expected_slippage, 4), 4)
    assert performance.cost_reporting["stored_slippage_estimated_unit"] == "QUOTE_PRICE_DELTA"
    assert performance.cost_reporting["exit_slippage_applied"] is False
    context = SimpleNamespace(paper_engine=paper_engine)
    detail = (await paper_get_order(closed.paper_order_id, context))["data"]
    listing = (await paper_list_orders(context))["data"]
    assert detail["order"] == listing["orders"][0] == snapshot
    assert detail["cost_report"]["slippage_cost_quote_estimate"] == pytest.approx(expected_slippage)
    assert listing["order_costs"][0] == detail["cost_report"]

    class DurableOrders:
        async def load_paper_orders(self):
            return [closed.model_copy(deep=True)]

    restored = PaperTradingEngine(audit_service, risk_manager, repository=DurableOrders())
    await restored.initialize()
    assert restored.performance().model_dump(exclude={"max_drawdown_percent"}) == performance.model_dump(exclude={"max_drawdown_percent"})
    await restored.initialize()
    assert restored.performance().slippage_total == performance.slippage_total
    assert restored.closed_orders[0].model_dump(mode="json") == snapshot


async def test_realistic_cost_report_does_not_add_volume_impact_twice(audit_service, risk_manager):
    engine = PaperTradingEngine(audit_service, risk_manager, execution_model=RealisticExecutionModel(
        BacktestExecutionAssumptions(half_spread_bps=2, base_slippage_bps=3, volume_impact_bps=20)))
    decision = make_decision()
    check = await risk_manager.check(decision, entry_price=100, atr=1)
    order = await engine.create_order(decision, check, current_price=100, market_candle=make_candle(100, volume=100))
    closed = await engine.close_order(order.paper_order_id, 100, "TEST_END", market_candle=make_candle(100, volume=100))
    perf = engine.performance()
    assert perf.volume_impact_total > 0
    assert perf.total_execution_cost == round(perf.fees_total + perf.spread_total + perf.slippage_total + perf.funding_total, 4)
    ledger = engine._execution_costs[closed.paper_order_id]
    report = engine.order_cost_report(closed)
    assert report["slippage_cost_quote_estimate"] == round(ledger.spread + ledger.slippage, 8)
    assert report["includes_spread"] is True
    assert perf.cost_reporting["volume_impact_included_in_slippage"] is True


@pytest.mark.parametrize("runtime,evidence,declared,effective", [
    ("abc", "abc", "PASSED", "PASSED"),
    ("abc", "def", "PASSED", "REVISION_MISMATCH"),
    ("abc", "", "PASSED", "REVISION_UNVERIFIED"),
    ("", "abc", "PASSED", "REVISION_UNVERIFIED"),
    ("", "", "PASSED", "REVISION_UNVERIFIED"),
    ("abc", "def", "FAILED", "FAILED"),
    ("abc", "def", "UNSET", "UNSET"),
])
async def test_release_status_requires_current_revision(monkeypatch, paper_state_machine, runtime, evidence, declared, effective):
    monkeypatch.setenv("RELEASE_EVIDENCE_STATUS", declared)
    monkeypatch.setenv("RELEASE_EVIDENCE_SOURCE_REVISION", evidence)
    monkeypatch.setenv("RELEASE_EVIDENCE_BUNDLE_ID", "original-bundle")
    context = SimpleNamespace(
        settings=Settings(app_source_revision=runtime), state_machine=paper_state_machine,
        market_connected=False, repository=None,
        oms_service=SimpleNamespace(target_environment=ExecutionEnvironment.PAPER, target_exchange=Exchange.BINANCE),
        orchestrator=SimpleNamespace(strategy_engine=StrategyEngine()),
    )
    result = (await system_status(context))["data"]
    release = result["release_evidence"]
    assert release["status"] == effective
    assert release["declared_status"] == declared
    assert release["matches_runtime_revision"] is bool(runtime and runtime == evidence)
    assert release["source_revision"] == (evidence or None)
    assert release["bundle_id"] == "original-bundle"
    assert result["oms"]["live_execution_available"] is False

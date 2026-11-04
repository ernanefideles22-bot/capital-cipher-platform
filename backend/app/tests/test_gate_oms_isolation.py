"""Gate owns only its venue/environment in a shared disposable database."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.core.errors import ValidationError
from app.database.models import ExecutionCommandModel, OMSOrderModel
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.paper import PaperExecutionAdapter
from app.oms.service import OMSService
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionCommand, ExecutionEnvironment
from app.tests.test_gate_testnet_execution import order


@pytest.fixture
async def mixed(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'venues.db'}")
    await db.create_all()
    repo = Repository(db)
    orders, commands = [], []
    now = datetime.now(timezone.utc)
    async with db.session() as session, session.begin():
        for i, (venue, environment) in enumerate([
            (Exchange.GATE, ExecutionEnvironment.TESTNET),
            (Exchange.GATE, ExecutionEnvironment.PAPER),
            (Exchange.BINANCE, ExecutionEnvironment.TESTNET),
            (Exchange.BINANCE, ExecutionEnvironment.PAPER),
            (Exchange.BYBIT, ExecutionEnvironment.TESTNET),
        ]):
            row = order(oms_order_id=str(uuid4()), client_order_id=str(uuid4()),
                        approval_id=uuid4().hex * 2, exchange=venue, environment=environment,
                        created_at=now + timedelta(seconds=i))
            command = ExecutionCommand(oms_order_id=row.oms_order_id, command_type="SUBMIT")
            values = repo._execution_command_values(command)
            if i == 4:
                values.update(status="LEASED", attempt_count=1, leased_by="bybit-worker",
                              lease_expires_at=now - timedelta(seconds=1))
            session.add(OMSOrderModel(**repo._oms_order_values(row)))
            session.add(ExecutionCommandModel(**values))
            orders.append(row)
            commands.append(command)
    try:
        yield db, repo, orders, commands
    finally:
        await db.dispose()


def service(repo, paper_engine, risk_manager, audit_service, environment=ExecutionEnvironment.TESTNET):
    # No dispatch occurs: the adapter is a local sentinel dependency only.
    return OMSService(target_exchange=Exchange.GATE, target_environment=environment,
                      repository=repo, paper_engine=paper_engine, risk_manager=risk_manager,
                      audit_service=audit_service,
                      adapters={(Exchange.GATE, environment): PaperExecutionAdapter(paper_engine)})


async def test_gate_initialize_list_id_and_cancel_cannot_access_foreign_rows(
    mixed, paper_engine, risk_manager, audit_service,
):
    _, repo, orders, _ = mixed
    oms = service(repo, paper_engine, risk_manager, audit_service)
    await oms.initialize()
    assert set(oms._orders) == {orders[0].oms_order_id}
    assert [o.oms_order_id for o in await oms.list_orders(limit=1)] == [orders[0].oms_order_id]
    assert await oms.get_order(orders[0].oms_order_id) is not None
    for other in orders[1:]:
        assert await oms.get_order(other.oms_order_id) is None
        with pytest.raises(ValidationError, match="does not exist"):
            await oms.queue_cancel(other.oms_order_id)


async def test_gate_claim_preserves_foreign_pending_and_expired_commands(
    mixed, paper_engine, risk_manager, audit_service,
):
    db, repo, orders, commands = mixed
    oms = service(repo, paper_engine, risk_manager, audit_service)
    claimed = await oms._claim_command()
    assert claimed[0].command_id == commands[0].command_id
    assert claimed[1].exchange is Exchange.GATE
    assert await oms._claim_command() is None
    async with db.session() as session:
        for command in commands[1:4]:
            row = await session.get(ExecutionCommandModel, command.command_id)
            assert (row.status, row.attempt_count) == ("PENDING", 0)
        expired = await session.get(ExecutionCommandModel, commands[4].command_id)
        assert (expired.status, expired.leased_by, expired.last_error_type) == ("LEASED", "bybit-worker", None)
        assert (await session.get(OMSOrderModel, orders[4].oms_order_id)).status == "PENDING_SUBMISSION"


async def test_gate_paper_never_leases_commands(mixed, paper_engine, risk_manager, audit_service):
    db, repo, _, commands = mixed
    oms = service(repo, paper_engine, risk_manager, audit_service, ExecutionEnvironment.PAPER)
    assert await oms._claim_command() is None
    async with db.session() as session:
        assert (await session.get(ExecutionCommandModel, commands[1].command_id)).status == "PENDING"


async def test_foreign_in_memory_entries_are_not_visible_or_leased(paper_engine, risk_manager, audit_service):
    oms = service(None, paper_engine, risk_manager, audit_service, ExecutionEnvironment.PAPER)
    foreign = order(exchange=Exchange.BINANCE, environment=ExecutionEnvironment.PAPER)
    command = ExecutionCommand(oms_order_id=foreign.oms_order_id, command_type="SUBMIT")
    oms._orders[foreign.oms_order_id] = foreign
    oms._commands[command.command_id] = command
    assert await oms.get_order(foreign.oms_order_id) is None
    assert await oms.list_orders() == []
    assert await oms._claim_command() is None
    assert oms._commands[command.command_id].attempt_count == 0

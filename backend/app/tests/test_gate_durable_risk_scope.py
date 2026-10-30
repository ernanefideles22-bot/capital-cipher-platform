"""Reproduce mixed PAPER/Gate approval admission without changing PAPER history."""

from uuid import uuid4

import pytest

from app.database.repositories.repository import _exposure_snapshot_hash
from app.schemas.common import Exchange, OrderSide, PaperOrderStatus
from app.schemas.paper import PaperOrder
from app.tests.test_gate_oms_connection import flow, analysis
from app.tests.test_gate_demo_release import wire


async def add_paper_positions(repository):
    orders = []
    for side in (OrderSide.BUY, OrderSide.SELL):
        item = PaperOrder(
            decision_id=str(uuid4()),
            risk_check_id=str(uuid4()),
            correlation_id=str(uuid4()),
            exchange=Exchange.BINANCE,
            symbol="BTCUSDT",
            timeframe="1h",
            strategy="DAY_1H",
            side=side,
            entry_price=80000,
            stop_loss=79000,
            take_profit=82000,
            position_size=100,
            leverage=1,
            status=PaperOrderStatus.FILLED,
        )
        await repository.save_paper_order(item)
        orders.append(item)
    return orders


@pytest.mark.asyncio
async def test_gate_owner_canary_admission_ignores_foreign_paper_but_preserves_it(flow):
    connection, venue, _, _ = flow
    await add_paper_positions(connection.repository)
    before = [
        o.model_dump(mode="json")
        for o in await connection.repository.load_paper_orders()
    ]
    release = wire(connection)
    result = await connection.process_analysis(analysis())
    assert result["status"] == "QUEUED"
    assert not venue.writes
    await connection.tick()
    assert venue.writes == ["entry", "stop"]
    await connection.tick()
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]
    assert release.status()["canary_evidence_sha256"]
    after = [
        o.model_dump(mode="json")
        for o in await connection.repository.load_paper_orders()
    ]
    assert before == after
    assert len(await connection.repository.load_open_position_exposures()) == 2
    assert not connection.risk.position_exposures()


@pytest.mark.asyncio
async def test_gate_durable_snapshot_retains_own_pending_reservation(flow):
    connection, _, _, _ = flow
    await add_paper_positions(connection.repository)
    result = await connection.process_analysis(analysis())
    assert result["status"] == "QUEUED"
    async with connection.repository._db.session() as session:
        global_positions = await connection.repository._durable_position_exposures(
            session, lock_paper=False
        )
        scoped = await connection.repository._durable_position_exposures(
            session, lock_paper=True, gate_testnet_only=True
        )
    assert len(global_positions) == 3
    assert len(scoped) == 1
    assert scoped[0].paper_order_id == "oms-reservation:" + result["oms_order_id"]
    assert _exposure_snapshot_hash(scoped) != _exposure_snapshot_hash([])

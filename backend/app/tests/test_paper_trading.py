"""Paper trading tests — mandatory scenarios from docs/22."""

from __future__ import annotations

import pytest

from app.core.errors import AuditError, RiskError, ValidationError
from app.schemas.common import CandidateAction, PaperOrderStatus, RiskStatus
from app.tests.conftest import make_candle, make_decision


async def approved_check(risk_manager, decision):
    return await risk_manager.check(decision, entry_price=100.0, atr=1.0)


async def test_create_and_fill_order(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    assert order.status == PaperOrderStatus.FILLED
    assert order.entry_price > 100.0  # BUY pays slippage
    assert order.fees_estimated > 0
    assert len(paper_engine.open_orders) == 1


async def test_order_without_approved_risk_check_is_impossible(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    blocked = check.model_copy(update={"risk_status": RiskStatus.BLOCKED, "approved": False})
    with pytest.raises(RiskError):
        await paper_engine.create_order(decision, blocked, current_price=100.0)


async def test_duplicate_order_same_decision_rejected(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    await paper_engine.create_order(decision, check, current_price=100.0)
    with pytest.raises(ValidationError):
        await paper_engine.create_order(decision, check, current_price=100.0)


async def test_stop_loss_triggers(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    crash = make_candle(order.stop_loss - 0.5, open_=99.0, high=99.5, low=order.stop_loss - 1)
    closed = await paper_engine.on_candle(crash)
    assert len(closed) == 1
    assert closed[0].exit_reason == "STOP_LOSS"
    assert closed[0].pnl < 0
    assert paper_engine.balance < paper_engine.initial_balance


async def test_take_profit_triggers(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    pump = make_candle(103.5, open_=100.5, high=order.take_profit + 1, low=100.0)
    closed = await paper_engine.on_candle(pump)
    assert len(closed) == 1
    assert closed[0].exit_reason == "TAKE_PROFIT"
    assert closed[0].pnl > 0


async def test_pnl_includes_fees_and_slippage(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    closed = await paper_engine.close_order(order.paper_order_id, order.entry_price)
    # Exit at exact entry: gross zero, net negative due to fees.
    assert closed.pnl < 0


async def test_losses_update_risk_state(paper_engine, risk_manager):
    for _ in range(2):
        decision = make_decision()
        check = await approved_check(risk_manager, decision)
        order = await paper_engine.create_order(decision, check, current_price=100.0)
        await paper_engine.close_order(order.paper_order_id, order.stop_loss, "STOP_LOSS")
    assert risk_manager.state.consecutive_losses == 2


async def test_sell_order_stop_and_direction(paper_engine, risk_manager):
    decision = make_decision(action=CandidateAction.SELL)
    check = await risk_manager.check(decision, entry_price=100.0, atr=1.0)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    assert order.stop_loss > order.entry_price
    spike = make_candle(102.0, open_=100.0, high=order.stop_loss + 1, low=99.9)
    closed = await paper_engine.on_candle(spike)
    assert closed[0].exit_reason == "STOP_LOSS"
    assert closed[0].pnl < 0


async def test_performance_metrics(paper_engine, risk_manager):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    await paper_engine.close_order(order.paper_order_id, order.take_profit, "TAKE_PROFIT")
    perf = paper_engine.performance()
    assert perf.closed_trades == 1
    assert perf.wins == 1
    assert perf.win_rate == 100.0
    assert perf.net_pnl > 0
    assert perf.fees_total > 0


async def test_close_fails_without_mutating_account_when_audit_fails(
    paper_engine,
    risk_manager,
    audit_service,
):
    decision = make_decision()
    check = await approved_check(risk_manager, decision)
    order = await paper_engine.create_order(
        decision,
        check,
        current_price=100.0,
    )
    initial_balance = paper_engine.balance
    audit_service.fail_mode = True

    with pytest.raises(AuditError):
        await paper_engine.close_order(
            order.paper_order_id,
            order.take_profit,
            "TAKE_PROFIT",
        )

    assert order.paper_order_id in paper_engine.open_orders
    assert paper_engine.closed_orders == []
    assert paper_engine.balance == initial_balance

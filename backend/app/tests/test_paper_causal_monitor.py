"""A position may only be monitored by a complete post-entry candle."""
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from app.core.errors import ValidationError
from app.schemas.common import CandidateAction, Exchange
from app.tests.conftest import make_candle, make_decision


async def opened(engine, risk, action=CandidateAction.BUY):
    decision = make_decision(action=action)
    check = await risk.check(decision, entry_price=100, atr=1)
    return await engine.create_order(decision, check, current_price=100)


@pytest.mark.parametrize("timeframe,seconds", [("1m",60),("15m",900),("1h",3600),("4h",14400)])
@pytest.mark.parametrize("relative_close", ["older", "equal", "partial"])
async def test_pre_entry_ohlc_cannot_close_or_accrue_funding(
    paper_engine, risk_manager, timeframe, seconds, relative_close,
):
    order = await opened(paper_engine, risk_manager)
    offsets = {"older":-1, "equal":0, "partial":seconds-1}
    candle = make_candle(80, open_=80, high=101, low=79, timeframe=timeframe,
        closed_at=order.opened_at + timedelta(seconds=offsets[relative_close]))
    paper_engine._accrue_funding = Mock()
    before = paper_engine.performance().model_dump()
    assert await paper_engine.on_candle(candle) == []
    assert order.paper_order_id in paper_engine.open_orders
    assert paper_engine.performance().model_dump() == before
    paper_engine._accrue_funding.assert_not_called()


@pytest.mark.parametrize("action,gap", [(CandidateAction.BUY,80),(CandidateAction.SELL,120)])
@pytest.mark.parametrize("timeframe,seconds", [("1m",60),("15m",900),("1h",3600),("4h",14400)])
async def test_complete_post_entry_bar_keeps_adverse_gap_fill(
    paper_engine, risk_manager, action, gap, timeframe, seconds,
):
    order = await opened(paper_engine, risk_manager, action)
    candle = make_candle(gap, open_=gap, high=gap+1, low=gap-1, timeframe=timeframe,
        closed_at=order.opened_at + timedelta(seconds=seconds))
    closed, = await paper_engine.on_candle(candle)
    assert closed.exit_price == gap
    assert closed.exit_reason == "STOP_LOSS"
    assert closed.closed_at == candle.closed_at


async def test_full_post_entry_bar_retains_stop_first_ambiguity(paper_engine,risk_manager):
    order = await opened(paper_engine,risk_manager)
    candle = make_candle(100,open_=100,low=order.stop_loss-1,high=order.take_profit+1,
        closed_at=order.opened_at+timedelta(minutes=15))
    closed, = await paper_engine.on_candle(candle)
    assert closed.exit_reason == "STOP_LOSS"
    assert closed.exit_price == order.stop_loss


async def test_other_exchange_cannot_close_position(paper_engine,risk_manager):
    order = await opened(paper_engine,risk_manager)
    candle = make_candle(80,open_=80,low=79,high=81,
        closed_at=order.opened_at+timedelta(minutes=15)).model_copy(update={"exchange":Exchange.BYBIT})
    assert await paper_engine.on_candle(candle) == []
    assert order.paper_order_id in paper_engine.open_orders


async def test_unknown_duration_rejected_before_monitoring(paper_engine,risk_manager):
    order = await opened(paper_engine,risk_manager)
    candle = make_candle(80,open_=80,high=81,low=79,
        closed_at=order.opened_at+timedelta(days=1)).model_copy(update={"timeframe":"unknown"})
    with pytest.raises(ValidationError,match="timeframe"):
        await paper_engine.on_candle(candle)
    assert order.paper_order_id in paper_engine.open_orders


@pytest.mark.parametrize("side,opened_at,closed_at,timeframe,stop,open_,high,low,close", [
    (CandidateAction.BUY, "2024-01-01T08:59:59.999+00:00", "2024-01-01T08:59:59.999+00:00", "1h", 98, 96, 104, 96, 101),
    (CandidateAction.SELL, "2024-01-01T02:59:59.999+00:00", "2024-01-01T03:59:59.999+00:00", "4h", 102, 105, 106, 97, 99),
    (CandidateAction.SELL, "2024-01-01T11:14:59.999+00:00", "2024-01-01T11:59:59.999+00:00", "4h", 102, 104, 106, 96, 97),
])
async def test_larger_pre_entry_candle_cannot_manufacture_adverse_gap(
    paper_engine, risk_manager, side, opened_at, closed_at, timeframe,
    stop, open_, high, low, close,
):
    # Synthetic cases: same close, one hour later inside a 4h bar, and
    # 45 minutes later inside a 4h bar. None provides a post-entry open.
    order = await opened(paper_engine, risk_manager, side)
    order.opened_at = datetime.fromisoformat(opened_at)
    order.stop_loss = stop
    order.take_profit = None
    candle = make_candle(close, open_=open_, high=high, low=low,
        timeframe=timeframe, closed_at=datetime.fromisoformat(closed_at))
    assert await paper_engine.on_candle(candle) == []
    assert order.paper_order_id in paper_engine.open_orders


async def test_position_without_entry_timestamp_fails_closed(paper_engine, risk_manager):
    order = await opened(paper_engine, risk_manager)
    candle = make_candle(80, open_=80, high=81, low=79,
        closed_at=order.opened_at+timedelta(minutes=15))
    order.opened_at = None
    with pytest.raises(ValidationError, match="entry timestamp"):
        await paper_engine.on_candle(candle)
    assert order.paper_order_id in paper_engine.open_orders

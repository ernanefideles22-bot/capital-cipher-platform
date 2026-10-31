"""D2.1: venue-aware historical fills and shared Gate-like one-way ledger."""
from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.venue_research_execution import (
    ContractSpec, Quote, HistoricalQuoteTape, OrderIntent, FundingEvent,
    OneWayPortfolio, replay_venue_portfolio,
)

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
SPEC = ContractSpec("BTC_USDT", multiplier=0.001, tick_size=0.1, maximum_contracts=50)


def quote(i: int, *, received_delay: int = 0, bid=100_000, ask=100_001,
          fee_bps=5.0, bid_depth=10, ask_depth=10) -> Quote:
    t = BASE + timedelta(seconds=i)
    return Quote(
        quote_id=f"quote-{i}", contract=SPEC.symbol,
        observed_at=t, received_at=t + timedelta(seconds=received_delay),
        bid=bid, ask=ask, bid_depth_contracts=bid_depth,
        ask_depth_contracts=ask_depth, taker_fee_bps=fee_bps,
    )


def intent(i: int, side="BUY", count=1, *, strategy="SCALP_15M",
           reduce_only=False, sequence=0) -> OrderIntent:
    t = BASE + timedelta(seconds=i)
    return OrderIntent(
        event_id=f"order-{i}-{sequence}", strategy=strategy,
        contract=SPEC.symbol, side=side, contracts=count,
        signal_received_at=t - timedelta(milliseconds=1),
        execution_at=t, sequence=sequence, reduce_only=reduce_only,
    )


def tape(*quotes: Quote, **kwargs) -> HistoricalQuoteTape:
    return HistoricalQuoteTape(SPEC, quotes, max_age_seconds=20, **kwargs)


def test_prevents_lookahead_quote_and_stale_quotes():
    t = tape(quote(10), quote(20))
    with pytest.raises(ValueError, match="historically available"):
        t.simulate(intent(9))
    with pytest.raises(ValueError, match="stale"):
        t.simulate(intent(42))
    assert t.at(BASE + timedelta(seconds=10)).quote_id == "quote-10"
    with pytest.raises(ValueError, match="ordered"):
        tape(quote(20), quote(10))
    with pytest.raises(ValueError, match="observed after"):
        quote(3, received_delay=-1)


def test_only_quotes_received_on_or_before_execution_are_used():
    t = tape(quote(0), quote(10, received_delay=5))
    assert t.simulate(intent(12)).quote_id == "quote-0"
    assert t.simulate(intent(15)).quote_id == "quote-10"


def test_spread_slippage_fee_and_impact_not_double_counted():
    t = tape(quote(10, bid=100, ask=102, fee_bps=5, ask_depth=1),
             extra_slippage_bps=12, impact_bps_per_extra_top_level=10)
    f = t.simulate(intent(10, count=3))
    assert f.price > 102
    assert f.spread_cost == pytest.approx(0.003, rel=1e-8)
    assert f.adverse_slippage_cost > 0
    assert f.fee == pytest.approx(f.price * 0.003 * 0.0005, rel=1e-7)
    short = t.simulate(intent(10, "SELL", count=3, sequence=1))
    assert short.price < 100
    assert short.fee > 0


def test_no_unrealistically_cheap_research_or_duplicate_quotes():
    with pytest.raises(ValueError, match="12 bps"):
        tape(quote(10), extra_slippage_bps=0)
    with pytest.raises(ValueError, match="Duplicate"):
        tape(quote(10), Quote(**{**quote(11).__dict__, "quote_id": "quote-10"}))


def test_opposite_strategies_net_then_reverse_exactly_once():
    t = tape(quote(1), quote(2), quote(3), quote(4))
    commands = [
        intent(1, "BUY", count=3, strategy="SCALP_15M"),
        intent(2, "SELL", count=2, strategy="DAY_1H"),
        intent(3, "SELL", count=2, strategy="SWING_4H"),
        intent(4, "BUY", count=1, strategy="SCALP_15M", reduce_only=True),
    ]
    result = replay_venue_portfolio(t, commands)
    assert [x.signed_contracts for x in result.snapshots] == [3, 1, -1, 0]
    assert result.final_state.signed_contracts == 0
    assert result.final_state.average_entry_price == 0
    assert result.final_state.handled_orders == 4
    assert result.final_state.fees > 0
    assert result.final_state.realized_net == pytest.approx(
        result.final_state.realized_gross - result.final_state.fees
        - result.final_state.funding_cost,
    )
    assert not result.execution_authorized
    assert result.mode == "OFFLINE_ONE_WAY_RESEARCH"


def test_reduce_only_cannot_open_increase_or_reverse():
    t = tape(quote(1), quote(2))
    with pytest.raises(ValueError, match="reduce_only"):
        replay_venue_portfolio(t, [intent(1, "BUY", reduce_only=True)])
    with pytest.raises(ValueError, match="reduce_only"):
        replay_venue_portfolio(t, [
            intent(1, "BUY", 1),
            intent(2, "SELL", 2, reduce_only=True),
        ])
    with pytest.raises(ValueError, match="reduce_only"):
        replay_venue_portfolio(t, [
            intent(1, "BUY", 1),
            intent(2, "BUY", 1, reduce_only=True),
        ])


def test_insufficient_1x_equity_and_leverage_rejected():
    with pytest.raises(ValueError, match="1x"):
        OneWayPortfolio(SPEC, leverage=2)
    with pytest.raises(ValueError, match="Insufficient"):
        replay_venue_portfolio(tape(quote(1)), [intent(1, count=2)],
                               starting_equity=50)


def test_funding_is_signed_and_only_applies_once_at_settlement():
    t = tape(quote(1), quote(2), quote(3))
    funding = FundingEvent(
        funding_id="funding-1", contract=SPEC.symbol,
        settled_at=BASE + timedelta(seconds=2),
        received_at=BASE + timedelta(seconds=2),
        mark_price=100000, rate=0.001,
    )
    long_result = replay_venue_portfolio(t, [
        intent(1, "BUY", 2), intent(3, "SELL", 2, reduce_only=True)
    ], funding_events=[funding])
    short_result = replay_venue_portfolio(t, [
        intent(1, "SELL", 2), intent(3, "BUY", 2, reduce_only=True)
    ], funding_events=[funding])
    assert long_result.final_state.funding_cost == pytest.approx(0.2)
    assert short_result.final_state.funding_cost == pytest.approx(-0.2)
    assert long_result.final_state.handled_funding == 1
    with pytest.raises(ValueError, match="Duplicate funding"):
        replay_venue_portfolio(t, [intent(1)], funding_events=[funding, funding])


def test_future_funding_event_and_ambiguous_order_priority_rejected():
    t = tape(quote(1), quote(2))
    f = FundingEvent(
        funding_id="future-1", contract=SPEC.symbol,
        settled_at=BASE + timedelta(seconds=2),
        received_at=BASE + timedelta(seconds=3),
        mark_price=100000, rate=0.0001,
    )
    with pytest.raises(ValueError, match="not known"):
        replay_venue_portfolio(t, [intent(1)], funding_events=[f])
    with pytest.raises(ValueError, match="Ambiguous"):
        replay_venue_portfolio(t, [
            intent(1, "BUY", strategy="SCALP_15M"),
            intent(1, "SELL", strategy="DAY_1H"),
        ])


def test_order_signal_after_fill_rejected():
    t = BASE + timedelta(seconds=1)
    with pytest.raises(ValueError, match="before the signal"):
        OrderIntent("late", "SCALP", SPEC.symbol, "BUY", 1,
                    signal_received_at=t + timedelta(seconds=1),
                    execution_at=t, sequence=0)

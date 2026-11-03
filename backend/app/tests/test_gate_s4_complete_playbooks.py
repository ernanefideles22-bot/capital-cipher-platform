"""Hand-labelled setups and independent execution/clock/risk oracles."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import sin, pi

import pytest

from app.schemas.market import Candle
from app.strategy.gate_s2_replay import Friction
from app.strategy.gate_s4_complete_playbooks import PLAYBOOKS, PlaybookDecision, PlaybookReader
from app.strategy.gate_s4_playbook_replay import replay_playbook


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles(n=270, mode="flat"):
    result = []
    for i in range(n):
        close = (100 + i * .005 if mode == "trend" else
                 100 + .8 * sin(i * pi / 5) if mode == "range" else 100.)
        stamp = START + timedelta(minutes=15 * (i + 1))
        spread = .15 if mode == "range" else .5
        result.append(Candle(exchange="GATE", symbol="BTCUSDT", timeframe="15m", open=close,
            high=close + spread, low=close - spread, close=close, volume=100., closed_at=stamp,
            received_at=stamp + timedelta(seconds=1)))
    return result


def update(bar, o, h, l, c, volume=100):
    return bar.model_copy(update={"open": o, "high": h, "low": l, "close": c, "volume": volume})


def mirror(bars):
    return [update(b, 200 - b.open, 200 - b.low, 200 - b.high, 200 - b.close, b.volume) for b in bars]


def retest_fixture():
    bars = candles(mode="trend")
    upper = max(b.high for b in bars[200:220])
    bars[220] = update(bars[220], upper - .2, upper + .4, upper - .3, upper + .3, 220)
    bars[221] = update(bars[221], upper + .3, upper + .4, upper - .1, upper + .15)
    bars[222] = update(bars[222], upper + .15, upper + .7, upper + .1, upper + .5)
    return bars


def failure_fixture():
    bars = candles(mode="range")
    upper = max(b.high for b in bars[200:220])
    bars[220] = update(bars[220], upper - .05, upper + .35, upper - .15, upper + .3, 150)
    bars[221] = update(bars[221], upper + .1, upper + .2, upper - .2, upper - .1)
    return bars


@pytest.mark.parametrize("short", [False, True])
def test_closed_breakout_then_separate_retest_then_confirmation(short):
    bars = retest_fixture()
    if short:
        bars = mirror(bars)
    reader = PlaybookReader(bars, cap=240)
    policy = "BREAKOUT_RETEST_V1"
    prior = reader.decision(policy, 222, bars[221].closed_at)
    assert prior.action == "WAIT"
    d = reader.decision(policy, 223, bars[222].closed_at)
    assert d.action == ("SHORT" if short else "LONG")
    direction = -1 if short else 1
    risk = direction * (d.reference_price - d.stop_price)
    assert direction * (d.target_price - d.reference_price) / risk == pytest.approx(2)
    assert d.setup_index == 220 and d.setup_id
    assert not d.execution_authorized and not d.predictive_edge_proven


@pytest.mark.parametrize("long", [False, True])
def test_failed_breakout_returns_inside_prior_range_with_structural_stop(long):
    bars = failure_fixture()
    if long:
        bars = mirror(bars)
    d = PlaybookReader(bars, cap=240).decision("FAILED_BREAKOUT_V1", 222, bars[221].closed_at)
    assert d.action == ("LONG" if long else "SHORT")
    direction = 1 if long else -1
    assert direction * (d.target_price - d.reference_price) >= 1.25 * direction * (d.reference_price - d.stop_price)
    assert d.setup_index == 220


def test_unclosed_future_and_modified_tail_cannot_change_past_decision():
    bars = retest_fixture()
    modified = bars[:223] + [b.model_copy(update={"close": 10000, "high": 10001}) for b in bars[223:]]
    a = PlaybookReader(bars, cap=240)
    b = PlaybookReader(modified, cap=240)
    clock = bars[222].closed_at
    assert a.decision("BREAKOUT_RETEST_V1", 223, clock) == b.decision("BREAKOUT_RETEST_V1", 223, clock)
    with pytest.raises(ValueError):
        a.decision("BREAKOUT_RETEST_V1", 224, clock)
    with pytest.raises(ValueError):
        a.decision("BREAKOUT_RETEST_V1", 241, bars[-1].closed_at)


class FixtureReader:
    def __init__(self, *, side="LONG", count=222, repeated=False):
        self.side, self.count, self.repeated = side, count, repeated

    def decision(self, policy, count, clock):
        active = count == self.count or self.repeated
        return PlaybookDecision(policy, "1.0.0", "BTCUSDT", "15m", clock,
            self.side if active else "WAIT", 100., 1., 99. if self.side == "LONG" else 101.,
            102. if self.side == "LONG" else 98., 99.8 if self.side == "LONG" else 100.2,
            count - 3, "ONE_SETUP", "FIXTURE", False, False)


def run_fixture(bars, reader=None, **kwargs):
    return replay_playbook("BREAKOUT_RETEST_V1", bars, reader or FixtureReader(),
                           start=220, end=len(bars), segment="FIXTURE", **kwargs)


@pytest.mark.parametrize("short", [False, True])
def test_entry_is_strictly_after_closed_decision_and_ambiguous_bar_stops_first(short):
    bars = candles(240)
    bars[223] = update(bars[223], 100, 103, 97, 100)
    result = run_fixture(bars, FixtureReader(side="SHORT" if short else "LONG"), friction=Friction(0, 0, 0))
    trades = result["report"].trades
    assert len(trades) == 1
    t = trades[0]
    assert (t.decision_bar_index, t.entry_bar_index, t.exit_bar_index) == (221, 223, 223)
    assert bars[t.entry_bar_index - 1].closed_at > bars[t.decision_bar_index].closed_at + timedelta(microseconds=1)
    assert t.exit_reason == "STOP_FIRST" and t.net_pnl == pytest.approx(-2.5)
    assert result["strict_entry_clock_checks"] == 1


@pytest.mark.parametrize("case,expected", [("stop", "INVALIDATED_BEFORE_ENTRY"),
    ("target", "TARGET_ALREADY_TOUCHED_BEFORE_ENTRY"), ("drift", "ENTRY_MOVED_TOO_FAR")])
def test_setup_cannot_chase_a_move_or_enter_after_invalidation(case, expected):
    bars = candles(240)
    if case == "stop":
        bars[222] = update(bars[222], 100, 100.5, 98, 100)
    elif case == "target":
        bars[222] = update(bars[222], 100, 103, 99.5, 100)
    else:
        bars[223] = update(bars[223], 100.8, 101, 100.6, 100.9)
    result = run_fixture(bars)
    assert result["skip_counts"][expected] == 1
    assert not result["report"].trades


def test_setup_is_consumed_once_and_cooldown_is_enforced():
    bars = candles(260)
    bars[223] = update(bars[223], 100, 103, 99.5, 100)
    result = run_fixture(bars, FixtureReader(repeated=True), friction=Friction(0, 0, 0))
    assert len(result["report"].trades) == 1
    assert result["skip_counts"]["COOLDOWN"] == 4
    assert result["skip_counts"]["SETUP_ALREADY_CONSUMED"] > 0


def test_thesis_invalidation_and_time_limit_exit_at_later_open():
    bars = candles(260)
    bars[224] = update(bars[224], 100, 100.2, 99.5, 99.7)
    result = run_fixture(bars, friction=Friction(0, 0, 0))
    t = result["report"].trades[0]
    assert t.exit_reason == "THESIS_INVALIDATION_NEXT_OPEN" and t.exit_bar_index == 226
    untouched = run_fixture(candles(260), friction=Friction(0, 0, 0))["report"].trades[0]
    assert untouched.exit_reason == "TIME_LIMIT_NEXT_OPEN" and untouched.exit_bar_index == 243


def test_costs_reduce_net_and_extra_delay_adds_a_bar_without_future_features():
    bars = candles(260)
    clean = run_fixture(bars, friction=Friction(0, 0, 0))["report"].trades[0]
    costly = run_fixture(bars)["report"].trades[0]
    late = run_fixture(bars, extra_delay=1)["report"].trades[0]
    assert costly.fees_paid > 0 and costly.funding_paid > 0 and costly.net_pnl < clean.net_pnl
    assert late.entry_bar_index == costly.entry_bar_index + 1
    assert late.decision_bar_index == costly.decision_bar_index
    assert not run_fixture(bars)["execution_authorized"]

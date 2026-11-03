"""Hand-built trend, affordability, execution and journal boundary oracles."""
import json
import math
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.market import Candle
from app.strategy.gate_s2_replay import Friction
from app.strategy.gate_s4_complete_playbooks import PlaybookDecision
from app.strategy import gate_s4_slow_momentum as rules
from app.strategy import gate_s4_slow_momentum_study as study
from app.strategy.gate_s4_slow_momentum_replay import replay_momentum

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def candles(n=280, trend=True, spread=.008):
    bars = []
    for i in range(n):
        close = 100 * math.exp(.0015 * i + .004 * math.sin(i)) if trend else 100.
        stamp = START + timedelta(hours=4 * (i + 1))
        bars.append(Candle(exchange="GATE", symbol="BTCUSDT", timeframe="4h", open=close,
            high=close * (1 + spread), low=close * (1 - spread), close=close,
            volume=100, closed_at=stamp, received_at=stamp + timedelta(seconds=1)))
    return bars


def update(b, o, h, l, c):
    return b.model_copy(update=dict(open=o, high=h, low=l, close=c))


@pytest.mark.parametrize("short", [False, True])
def test_daily_own_price_momentum_has_mirrored_risk_and_fixed_three_r_target(short):
    bars = candles()
    if short:
        bars = [update(b, 10000/b.open, 10000/b.low, 10000/b.high, 10000/b.close) for b in bars]
    reader = rules.MomentumReader(bars, cap=250)
    d = reader.decision(rules.POLICY_ID, 240, bars[239].closed_at)
    assert d.action == ("SHORT" if short else "LONG")
    assert abs(d.reference_price-d.stop_price) == pytest.approx(2*d.atr)
    assert abs(d.target_price-d.reference_price) == pytest.approx(6*d.atr)
    assert d.setup_index == 239 and d.setup_id
    assert not d.execution_authorized and not d.predictive_edge_proven
    assert reader.decision(rules.POLICY_ID, 241, bars[240].closed_at).reason == "NOT_DAILY_OPPORTUNITY"


def test_cost_budget_includes_entire_maximum_hold_funding_and_can_only_abstain():
    assert rules.planning_cost_fraction() == pytest.approx(.0078)  # 48bps + 15*2bps.
    bars = candles(spread=.0001)
    d = rules.MomentumReader(bars, cap=250).decision(rules.POLICY_ID, 240, bars[239].closed_at)
    assert d.action == "WAIT" and d.reason == "PLANNED_COST_TOO_LARGE_FOR_TARGET"
    flat = candles(trend=False)
    d = rules.MomentumReader(flat, cap=250).decision(rules.POLICY_ID, 240, flat[239].closed_at)
    assert d.action == "WAIT" and d.reason == "TREND_WEAK_OR_VOLATILITY_DEGENERATE"


def test_decisions_match_independent_visible_log_return_and_no_tail_leakage():
    bars = candles()
    a = rules.MomentumReader(bars, cap=250)
    changed = bars[:240] + [update(b, 10, 11, 9, 10) for b in bars[240:]]
    b = rules.MomentumReader(changed, cap=250)
    assert a.momentum(240) == pytest.approx(sum(math.log(bars[i].close/bars[i-1].close)
                                                 for i in range(120, 240)))
    assert a.decision(rules.POLICY_ID, 240, bars[239].closed_at) == b.decision(
        rules.POLICY_ID, 240, bars[239].closed_at)
    for count, clock in [(251, bars[-1].closed_at), (240, bars[238].closed_at),
                         (240, bars[239].closed_at + timedelta(hours=9))]:
        with pytest.raises(ValueError):
            a.decision(rules.POLICY_ID, count, clock)


def test_unsupported_markets_and_timeframes_fail_closed():
    for changes in ({"symbol":"OTHER"}, {"timeframe":"1h"}):
        with pytest.raises(ValueError):
            rules.MomentumReader([b.model_copy(update=changes) for b in candles()], cap=250)


class FixtureReader:
    def __init__(self, short=False, broken=False, repeat=False):
        self.short, self.broken, self.repeat = short, broken, repeat

    def momentum(self, count):
        sign = -1 if self.short else 1
        return -.1 * sign if self.broken and count >= 225 else .1 * sign

    def decision(self, policy, count, clock):
        direction = -1 if self.short else 1
        return PlaybookDecision(policy, "1.0.0", "BTCUSDT", "4h", clock,
            ("SHORT" if self.short else "LONG") if count == 222 or self.repeat else "WAIT",
            100., 5., 100.-direction*10, 100.+direction*30, 100.-direction*20,
            count-1, "SAME_SETUP", "FIXTURE", False, False)


def replay(bars, reader=None, **kw):
    return replay_momentum(bars, reader or FixtureReader(), start=220, end=len(bars), segment="FIXTURE", **kw)


@pytest.mark.parametrize("short", [False, True])
def test_strict_clock_and_same_bar_stop_first_with_independent_risk_oracle(short):
    bars = candles(trend=False)
    bars[223] = update(bars[223], 100, 135, 65, 100)
    r = replay(bars, FixtureReader(short), friction=Friction(0,0,0))
    t, = r["report"].trades
    assert (t.decision_bar_index,t.entry_bar_index,t.exit_bar_index)==(221,223,223)
    assert bars[t.entry_bar_index-1].closed_at > bars[t.decision_bar_index].closed_at + timedelta(microseconds=1)
    assert t.exit_reason=="STOP_FIRST" and t.net_pnl==pytest.approx(-2.5)
    assert r["strict_entry_clock_checks"]==1


@pytest.mark.parametrize("kind,reason", [("stop","INVALIDATED_BEFORE_ENTRY"),
    ("target","TARGET_ALREADY_TOUCHED_BEFORE_ENTRY"), ("drift","ENTRY_MOVED_TOO_FAR")])
def test_pre_entry_invalidations_reject_the_opportunity(kind,reason):
    bars=candles(trend=False)
    if kind=="stop":bars[222]=update(bars[222],100,101,89,100)
    elif kind=="target":bars[222]=update(bars[222],100,131,99,100)
    else:bars[223]=update(bars[223],104,105,103,104)
    r=replay(bars)
    assert not r["report"].trades and r["skip_counts"][reason]==1


def test_dynamic_rolling_momentum_exit_time_limit_costs_delay_and_no_repeat():
    bars=candles(trend=False)
    broken=replay(bars,FixtureReader(broken=True),friction=Friction(0,0,0))["report"].trades[0]
    assert broken.exit_reason=="THESIS_INVALIDATION_NEXT_OPEN" and broken.exit_bar_index==226
    clean=replay(bars,friction=Friction(0,0,0))["report"].trades[0]
    costly=replay(bars)["report"].trades[0]
    late=replay(bars,extra_delay=1)["report"].trades[0]
    assert clean.exit_reason=="TIME_LIMIT_NEXT_OPEN" and clean.exit_bar_index==253
    assert costly.net_pnl < clean.net_pnl and costly.funding_paid>0 and costly.fees_paid>0
    assert late.entry_bar_index==costly.entry_bar_index+1 and late.decision_bar_index==costly.decision_bar_index
    r=replay(bars,FixtureReader(repeat=True))
    assert len(r["report"].trades)==1 and r["skip_counts"]["COOLDOWN"]==6
    assert r["skip_counts"]["SETUP_ALREADY_CONSUMED"]>0


def test_gap_stop_has_priority_over_scheduled_time_exit_and_can_exceed_planned_risk():
    bars=candles(trend=False)
    bars[253]=update(bars[253],80,81,79,80)
    t,=replay(bars,friction=Friction(0,0,0))["report"].trades
    assert t.exit_reason=="STOP_GAP" and t.net_pnl==pytest.approx(-5.)


def test_affordability_is_rechecked_at_actual_entry_without_tuning_the_budget():
    class TightTarget(FixtureReader):
        def decision(self,*args):
            return replace(super().decision(*args),atr=.65,stop_price=98.7,target_price=103.9)
    bars=candles(trend=False)
    bars[223]=update(bars[223],99.9,100,99.8,99.9)
    r=replay(bars,TightTarget(),friction=Friction(0,0,0))
    assert not r["report"].trades and r["skip_counts"]["EXECUTED_ENTRY_COST_BUDGET"]==1


def test_forced_boundary_mark_is_not_completed_trade_evidence():
    r=replay(candles(230,trend=False))["report"]
    assert r.trades[0].exit_reason=="END_WINDOW_ESTIMATE"
    assert study.lab.trade_metrics(r)["completed_trades"]==0
    assert r.net_pnl==pytest.approx(sum(t.net_pnl for t in r.trades))


def fixture_register(tmp_path,monkeypatch,available=False):
    catalog=tmp_path/"catalog.json";catalog.write_text("{}")
    rows=[]
    for symbol in rules.SYMBOLS:
        folder=tmp_path/f"{symbol}-4h";folder.mkdir();(folder/"source.json").write_text("{}")
        rows.append({"symbol":symbol,"timeframe":"4h","status":"AVAILABLE" if available else "UNAVAILABLE",
            "relative_path":f"{folder.name}/candles.json","sha256":"FIXTURE",
            "source_archive_sha256":study.hashlib.sha256(b"{}").hexdigest()})
    monkeypatch.setattr(study,"catalog_rows",lambda _:rows)
    root=tmp_path/"study";study.register(root,catalog)
    return root,catalog


def test_all_failures_retained_exclusive_registration_no_retry_and_no_promotion(tmp_path,monkeypatch):
    root,catalog=fixture_register(tmp_path,monkeypatch)
    study.run(root);r=study.review(root)
    assert r["complete"] and r["training_trials"]==r["training_failed"]==3
    assert not r["training_screen_passed"]
    assert all(not r[k] for k in ("independent_out_of_sample","execution_authorized",
        "final_reserve_evaluated","prospective_tracking_started","profitability_proven"))
    with pytest.raises(ValueError,match="already started"):study.run(root)
    with pytest.raises(FileExistsError):study.register(root,catalog)
    with study._connect(root) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0]==5
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):db.execute("DELETE FROM events")


def test_every_market_runs_only_first_sixty_percent_and_train_pass_never_opens_validation(tmp_path,monkeypatch):
    root,_=fixture_register(tmp_path,monkeypatch,available=True)
    monkeypatch.setattr(study,"load_real_dataset",lambda _:([None]*6000,"FIXTURE"))
    calls=[]
    monkeypatch.setattr(study,"MomentumReader",lambda bars,cap:calls.append(cap) or cap)
    def measure(bars,reader,end):
        assert reader==end==3600
        return {n:{"completed_trades":30,"closed_net_pnl":10.,"net_including_boundary_mark":10.,
            "profit_factor":1.3,"largest_win_share":.2,"candle_close_drawdown_pct":1.} for n in study.SCENARIOS}
    monkeypatch.setattr(study,"measure",measure)
    study.run(root);r=study.review(root)
    assert calls==[3600]*3 and r["training_screen_passed"]==3
    assert r["validation_trials_evaluated"]==0 and not r["automatic_promotion_authorized"]


def test_wrong_phase_rejected_and_source_change_blocks_frozen_journal(tmp_path,monkeypatch):
    root,_=fixture_register(tmp_path,monkeypatch)
    with study._connect(root) as db,db:study.lab._append(db,"TRAIN_FAILED",{"trial_id":"BAD"})
    with pytest.raises(ValueError,match="start missing"):study.review(root)
    monkeypatch.setattr(study,"fingerprint",lambda:"MODIFIED")
    with pytest.raises(ValueError,match="Frozen"):study.review(root)

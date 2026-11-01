"""S2: orderless versioned strategies, causal next-bar fills, stop-first and OOS."""
from datetime import datetime, timedelta, timezone
from math import sin, pi

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES, ResearchPlan, evaluate_strategy
from app.strategy import gate_s2_replay as replay

START=datetime(2026,10,1,tzinfo=timezone.utc)


def candles(n=210, *, timeframe="15m", price=100., trend=.04):
    step={"15m":900,"1h":3600,"4h":14400}[timeframe]
    out=[]
    for i in range(n):
        level=price+i*trend+.7*sin(i*pi/6)
        close=level+.2
        stamp=START+timedelta(seconds=step*(i+1))
        out.append(Candle(
            exchange=Exchange.GATE,symbol="BTCUSDT",timeframe=timeframe,
            open=level,high=max(level,close)+1,low=min(level,close)-1,
            close=close,volume=100,
            closed_at=stamp,received_at=stamp+timedelta(seconds=2),
        ))
    return out


def fake_entry_only(decision_index, distance=1., target=2.):
    def evaluate(strategy_id,bars,*,as_of,historical_replay=False):
        assert historical_replay is True
        assert all(b.closed_at <= as_of for b in bars)
        index=int((bars[-1].closed_at-START).total_seconds()/900)-1
        enter=index==decision_index
        return ResearchPlan(
            strategy_id=strategy_id,strategy_version="1.0.0",
            symbol=bars[0].symbol,timeframe=bars[0].timeframe,
            cutoff=as_of,action="LONG" if enter else "WAIT",
            stop_distance=distance if enter else None,
            target_distance=target if enter else None,
            reasoning="TEST_FIXTURE",execution_authorized=False,
        )
    return evaluate


def test_all_three_strategies_versioned_and_never_authorize_orders():
    data=candles(120)
    for name,spec in STRATEGIES.items():
        plan=evaluate_strategy(name,data,as_of=data[-1].received_at)
        assert plan.strategy_id==name
        assert plan.strategy_version=="1.0.0"
        assert plan.action in {"WAIT","LONG","SHORT"}
        assert plan.execution_authorized is False
        assert plan.predictive_edge_proven is False
        if plan.action!="WAIT":
            assert plan.stop_distance>0
            assert plan.target_distance>plan.stop_distance


def test_donchian_breakout_requires_closed_bar_and_volume():
    data=candles(120)
    last=data[-1]
    price=max(x.high for x in data[-21:-1])+5
    breakout=last.model_copy(update={
        "open":price-1,"high":price+1,"low":price-2,
        "close":price,"volume":250,
    })
    data[-1]=breakout
    good=evaluate_strategy("DONCHIAN_VOLUME_BREAKOUT_V1",data,
                            as_of=last.received_at)
    assert good.action=="LONG"
    assert good.target_distance==pytest.approx(2*good.stop_distance)
    data[-1]=breakout.model_copy(update={"volume":20})
    quiet=evaluate_strategy("DONCHIAN_VOLUME_BREAKOUT_V1",data,
                             as_of=last.received_at)
    assert quiet.action=="WAIT"


def test_signal_rejects_future_bar_future_receipt_and_gap():
    data=candles(120)
    with pytest.raises(ValueError,match="not-yet-received"):
        evaluate_strategy("EMA_TREND_PULLBACK_V1",data,as_of=data[-1].closed_at)
    with pytest.raises(ValueError,match="Gap"):
        evaluate_strategy("EMA_TREND_PULLBACK_V1",data[:70]+data[71:],
                          as_of=data[-1].received_at)
    with pytest.raises(ValueError,match="approved GATE"):
        bad=[b.model_copy(update={"exchange":Exchange.BINANCE}) for b in data]
        evaluate_strategy("EMA_TREND_PULLBACK_V1",bad,as_of=bad[-1].received_at)


def test_next_open_and_both_barrier_touch_uses_stop_first(monkeypatch):
    data=candles(100,trend=0)
    bar=data[81]
    data[81]=bar.model_copy(update={
        "open":100,"high":105,"low":95,"close":101,
    })
    monkeypatch.setattr(replay,"evaluate_strategy",fake_entry_only(80))
    report=replay.replay_segment(
        "DONCHIAN_VOLUME_BREAKOUT_V1",data,start_index=80,end_index=100,
        segment="TRAIN",friction=replay.Friction(0,0,0),
    )
    assert len(report.trades)==1
    t=report.trades[0]
    assert (t.decision_bar_index,t.entry_bar_index,t.exit_bar_index)==(80,81,81)
    assert t.entry_price==100
    assert t.exit_price==99
    assert t.exit_reason=="STOP_FIRST"
    assert t.net_pnl<0
    assert report.execution_authorized is False


def test_stop_gap_respects_actual_next_open_not_optimistic_stop(monkeypatch):
    data=candles(100,trend=0)
    data[81]=data[81].model_copy(update={
        "open":100,"close":100.2,"high":100.5,"low":99.5,
    })
    data[82]=data[82].model_copy(update={
        "open":90,"close":91,"high":92,"low":89,
    })
    monkeypatch.setattr(replay,"evaluate_strategy",fake_entry_only(80))
    report=replay.replay_segment(
        "EMA_TREND_PULLBACK_V1",data,start_index=80,end_index=100,
        segment="TRAIN",friction=replay.Friction(0,0,0),
    )
    assert report.trades[0].exit_reason=="STOP_GAP"
    assert report.trades[0].exit_price==90
    assert report.trades[0].net_pnl<0


def test_fee_slippage_and_adverse_funding_reduce_net_pnl(monkeypatch):
    data=candles(100,trend=0)
    data[81]=data[81].model_copy(update={
        "open":100,"high":102.5,"low":99.5,"close":101,
    })
    monkeypatch.setattr(replay,"evaluate_strategy",fake_entry_only(80))
    clean=replay.replay_segment(
        "EMA_TREND_PULLBACK_V1",data,start_index=80,end_index=100,
        segment="TRAIN",friction=replay.Friction(0,0,0),
    )
    friction=replay.replay_segment(
        "EMA_TREND_PULLBACK_V1",data,start_index=80,end_index=100,
        segment="TRAIN",friction=replay.Friction(6,6,1),
    )
    assert clean.trades[0].exit_reason=="TARGET"
    assert friction.trades[0].fees_paid>0
    assert friction.trades[0].funding_paid>0
    assert friction.trades[0].net_pnl<clean.trades[0].net_pnl


def test_daily_kill_blocks_new_trades_without_oms(monkeypatch):
    data=candles(110,trend=0)
    for j in range(81,110):
        data[j]=data[j].model_copy(update={
            "open":100,"high":100.5,"low":97,"close":100,
        })
    def every_bar(strategy_id,bars,*,as_of,historical_replay):
        return ResearchPlan(strategy_id,"1.0.0",bars[-1].symbol,bars[-1].timeframe,
                            as_of,"LONG",1.,2.,"TEST",False,False)
    monkeypatch.setattr(replay,"evaluate_strategy",every_bar)
    report=replay.replay_segment(
        "EMA_TREND_PULLBACK_V1",data,start_index=80,end_index=110,
        segment="TRAIN",friction=replay.Friction(0,0,0),
        risk=replay.ResearchRisk(risk_per_trade_pct=1.,max_daily_loss_pct=.1),
    )
    assert report.blocked_new_entries>0
    assert len(report.trades)<10
    assert report.execution_authorized is False


def test_oos_is_frozen_and_future_changes_do_not_change_train(monkeypatch):
    data=candles(360,trend=0)
    # Training profits occur after every next-open entry. OOS turns negative.
    split=int(len(data)*.6)
    for j in range(81,len(data)):
        price=100
        if j < split:
            hi,lo,close=104.,99.5,102.
        else:
            hi,lo,close=100.5,97.,99.
        data[j]=data[j].model_copy(update={
            "open":price,"high":hi,"low":lo,"close":close,
        })
    def every_bar(strategy_id,bars,*,as_of,historical_replay):
        assert historical_replay
        return ResearchPlan(strategy_id,"1.0.0",bars[-1].symbol,bars[-1].timeframe,
                            as_of,"LONG",1.,2.,"TEST",False,False)
    monkeypatch.setattr(replay,"evaluate_strategy",every_bar)
    risk=replay.ResearchRisk(min_selection_trades=3,min_oos_evidence_trades=30)
    a=replay.select_then_test_oos(data,friction=replay.Friction(0,0,0),risk=risk)
    assert a.selected_strategy in replay.STRATEGIES
    assert a.selection_status in {"EXPLORATORY_OOS_ONLY","OOS_INSUFFICIENT_TRADES"}
    assert a.oos_report is not None
    assert a.oos_report.net_pnl<0
    assert not a.automatic_promotion_authorized
    assert not a.order_submission_authorized
    newer=list(data)
    for j in range(split,len(newer)):
        newer[j]=newer[j].model_copy(update={"high":newer[j].high+4})
    b=replay.select_then_test_oos(newer,friction=replay.Friction(0,0,0),risk=risk)
    assert b.selected_strategy==a.selected_strategy
    assert b.train_reports==a.train_reports
    assert b.out_of_sample_start_index==split


def test_real_strategy_runs_without_any_broker_or_order_objects():
    data=candles(220)
    for name in sorted(STRATEGIES):
        report=replay.replay_segment(name,data,start_index=80,end_index=200,
                                     segment="TRAIN")
        assert report.bars_in_segment==120
        assert report.execution_authorized is False
        assert report.end_equity>0
        assert all(t.entry_bar_index>t.decision_bar_index for t in report.trades)
        assert all(t.stop_price>0 and t.target_price>0 for t in report.trades)
    result=replay.select_then_test_oos(data)
    assert result.automatic_promotion_authorized is False
    assert result.order_submission_authorized is False
    assert result.replay_is_not_real_execution is True


def test_bad_research_risk_and_friction_cannot_disable_costs_by_error():
    with pytest.raises(ValueError):
        replay.Friction(fee_bps_side=-1)
    with pytest.raises(ValueError):
        replay.ResearchRisk(risk_per_trade_pct=30)
    with pytest.raises(ValueError):
        replay.ResearchRisk(max_notional_pct=200)
    with pytest.raises(ValueError):
        replay.ResearchRisk(min_oos_evidence_trades=0)

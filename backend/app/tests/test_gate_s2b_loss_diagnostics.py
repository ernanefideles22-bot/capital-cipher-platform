"""S2B attribution identities and chronological, orderless walk-forward gates."""
from datetime import datetime,timedelta,timezone
from math import sin,pi

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import ResearchPlan
from app.strategy import gate_s2_replay as replay
from app.strategy.gate_s2_loss_diagnostics import attribute,expanding_walk_forward

T0=datetime(2026,10,1,tzinfo=timezone.utc)


def bars(n=800):
    output=[]
    for j in range(n):
        x=100+0.04*j+0.8*sin(j*pi/6)
        ts=T0+timedelta(minutes=15*(j+1))
        output.append(Candle(
            exchange=Exchange.GATE,symbol="BTCUSDT",timeframe="15m",
            open=x,high=x+1.2,low=x-1.2,close=x+.1,volume=120,
            closed_at=ts,received_at=ts+timedelta(seconds=2),
        ))
    return output


def stub_signals(strategy_id,candles,*,as_of,historical_replay):
    # Deterministic one entry every 10 bars, no access to future candles.
    assert historical_replay is True
    assert max(c.closed_at for c in candles) <= as_of
    idx=int((candles[-1].closed_at-T0).total_seconds()/900)-1
    chosen=(idx%10==0)
    return ResearchPlan(
        strategy_id=strategy_id,strategy_version="1.0.0",
        symbol=candles[0].symbol,timeframe=candles[0].timeframe,
        cutoff=as_of,action="LONG" if chosen else "WAIT",
        stop_distance=2 if chosen else None,
        target_distance=4 if chosen else None,
        reasoning="ISOLATED_TEST",execution_authorized=False,
    )


def test_all_real_replay_cost_components_reconcile():
    data=bars(220)
    assumptions=replay.Friction(6,6,1)
    for strategy in sorted(replay.STRATEGIES):
        report=replay.replay_segment(strategy,data,start_index=80,end_index=220,
                                     segment="DIAGNOSTIC",friction=assumptions)
        result=attribute(report,assumptions=assumptions)
        assert result.strategy_id==strategy
        assert result.net_pnl==pytest.approx(report.net_pnl,abs=1e-5)
        assert result.raw_gross_before_slippage-result.slippage_drag - (
            result.exchange_fees+result.funding_estimate
        )==pytest.approx(result.net_pnl,abs=4e-5)
        assert result.trades==len(report.trades)
        assert result.trading_authorized is False
        assert result.slippage_drag>=-1e-6
        assert result.exchange_fees>=0
        assert result.funding_estimate>=0


def test_causal_folds_have_disjoint_forward_windows():
    data=bars()
    result=expanding_walk_forward(data,initial_train_bars=400,oos_bars=120)
    assert result.total_folds==3
    assert [(f.training_end_index,f.oos_start_index,f.oos_end_index)
            for f in result.folds]==[
                (400,400,520),(520,520,640),(640,640,760)]
    assert all(not f.selection_uses_future_bars for f in result.folds)
    assert all(not f.oos_report or f.oos_report.segment=="WF_OOS"
               for f in result.folds)
    assert result.automatic_promotion_authorized is False
    assert result.execution_authorized is False
    assert result.profitability_proven is False


def test_walk_forward_no_trade_is_safe_abstention(monkeypatch):
    data=bars(600)
    def never(*args,**kwargs):
        original=args[2] if len(args)>2 else kwargs.get("as_of")
        return ResearchPlan(
            strategy_id=args[0],strategy_version="1.0.0",
            symbol=args[1][0].symbol,timeframe=args[1][0].timeframe,
            cutoff=original or args[1][-1].closed_at,
            action="WAIT",stop_distance=None,target_distance=None,
            reasoning="ABSTAIN",execution_authorized=False)
    monkeypatch.setattr(replay,"evaluate_strategy",never)
    result=expanding_walk_forward(data,initial_train_bars=400,oos_bars=100)
    assert result.total_folds==2
    assert result.folds_with_selection==0
    assert result.oos_completed_trades==0
    assert result.evidence_insufficient
    assert result.oos_net_pnl_sum_separate_accounts==0


def test_walk_forward_selection_is_invariant_to_future_oos_modification(monkeypatch):
    data=bars(680)
    monkeypatch.setattr(replay,"evaluate_strategy",stub_signals)
    risk=replay.ResearchRisk(min_selection_trades=3,min_oos_evidence_trades=30)
    baseline=expanding_walk_forward(
        data,initial_train_bars=400,oos_bars=120,
        friction=replay.Friction(0,0,0),risk=risk)
    future=list(data)
    # Change bars only AFTER first fold's OOS end. Training for first
    # fold and its following forward window must be absolutely unchanged.
    for j in range(520,len(future)):
        future[j]=future[j].model_copy(update={
            "high":future[j].high+3,"low":max(.01,future[j].low-3)})
    shifted=expanding_walk_forward(
        future,initial_train_bars=400,oos_bars=120,
        friction=replay.Friction(0,0,0),risk=risk)
    assert baseline.folds[0]==shifted.folds[0]
    assert baseline.execution_authorized is False
    assert shifted.automatic_promotion_authorized is False


def test_corrupted_trade_accounting_is_rejected(monkeypatch):
    data=bars(110)
    monkeypatch.setattr(replay,"evaluate_strategy",stub_signals)
    base=replay.replay_segment(
        "EMA_TREND_PULLBACK_V1",data,start_index=80,end_index=110,
        segment="DIAGNOSTIC",friction=replay.Friction())
    assert base.trades
    result=attribute(base,assumptions=replay.Friction())
    assert result.trades==len(base.trades)
    from dataclasses import replace
    corrupted=replace(base,trades=(replace(base.trades[0],net_pnl=999999),)
                      +base.trades[1:])
    with pytest.raises(ValueError,match="Corrupted"):
        attribute(corrupted,assumptions=replay.Friction())


@pytest.mark.parametrize("train,forward",[(10,120),(400,12),(1000,120)])
def test_invalid_fold_boundaries_fail_closed(train,forward):
    with pytest.raises(ValueError,match="Insufficient|Not enough"):
        expanding_walk_forward(bars(700),initial_train_bars=train,
                               oos_bars=forward)



def test_walk_forward_accepts_batch_observed_histories_without_faking_receipts():
    historical=bars(800)
    batch_receipt=historical[-1].closed_at+timedelta(seconds=5)
    historical=[bar.model_copy(update={"received_at":batch_receipt})
                for bar in historical]
    # This deliberately replicates the Gate public historical fetch:
    # all old bars are first observed at one single current request time.
    result=expanding_walk_forward(
        historical,initial_train_bars=400,oos_bars=120)
    assert result.total_folds==3
    assert result.profitability_proven is False
    assert result.automatic_promotion_authorized is False

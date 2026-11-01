"""S3: prospective decisions must precede hypothetical entry-bar opening."""
from datetime import datetime,timedelta,timezone
from dataclasses import replace

import pytest

from app.schemas.market import Candle
from app.schemas.common import Exchange
from app.strategy.gate_s3_forward_scorecard import ShadowForwardScorecard
from app.strategy.gate_s2e_live_shadow import S2E_POLICY,S2E_POLICY_COMMIT
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_replay import Friction

UTC=timezone.utc
BASE=datetime(2026,11,2,0,15,tzinfo=UTC)
STRATEGY="DONCHIAN_VOLUME_BREAKOUT_V1"


def candle(i,*,op=100.,high=101.,low=99.,close=100.,symbol="BTCUSDT",received_offset=5):
    at=BASE+timedelta(minutes=15*i)
    return Candle(
        exchange=Exchange.GATE,symbol=symbol,timeframe="15m",
        open=op,high=high,low=low,close=close,volume=100,
        closed_at=at,received_at=at+timedelta(seconds=received_offset))


def shadow(bar,*,proposed="WAIT",filtered=None,observed_offset=5):
    reported=bar.closed_at+timedelta(seconds=observed_offset)
    filtered=proposed if filtered is None else filtered
    rows=[]
    for name in sorted(STRATEGIES):
        action=proposed if name==STRATEGY else "WAIT"
        action_filtered=filtered if name==STRATEGY else "WAIT"
        rows.append({
            "strategy_id":name,
            "strategy_version":STRATEGIES[name].version,
            "timeframe":"15m",
            "proposed_action":action,
            "filtered_action":action_filtered,
            "stop_distance":2. if action!="WAIT" else None,
            "target_distance":4. if action!="WAIT" else None,
            "predictive_edge_proven":False,
            "execution_authorized":False,
        })
    return {
        "status":"READ_ONLY_CONFLUENCE_READY",
        "symbol":bar.symbol,"as_of_utc":reported.isoformat(),
        "policy_id":S2E_POLICY,"policy_commit":S2E_POLICY_COMMIT,
        "oms_touched":False,"source_decision_cohort_modified":False,
        "execution_authorized":False,
        "hypotheses":rows,
    }


def send(book,b,*,proposed="WAIT",filtered=None,offset=5):
    return book.observe(
        b,shadow(b,proposed=proposed,filtered=filtered,
                 observed_offset=offset),
        observed_at=b.closed_at+timedelta(seconds=offset))


def account(report,arm):
    return next(a for a in report["accounts"]
                if a["arm"]==arm and a["strategy"]==STRATEGY)


def test_no_retroactive_entry_to_a_bar_opened_before_signal_arrived():
    score=ShadowForwardScorecard(friction=Friction(0,0,0))
    a=send(score,candle(0),proposed="LONG",filtered="WAIT")
    assert account(a,"STANDALONE")["pending"]
    assert account(a,"STANDALONE")["completed_trades"]==0
    # At 00:30 OHLC is known, but bar opened at 00:15,
    # *before* the signal was observed at 00:15:05.
    b=send(score,candle(1))
    assert account(b,"STANDALONE")["pending"] is True
    assert not account(b,"STANDALONE")["open_position"]
    # At 00:45 the bar began at 00:30 (after 00:15:05):
    # the hypothetical entry is chronologically admissible.
    c=send(score,candle(2,high=101,low=99))
    assert account(c,"STANDALONE")["open_position"]
    assert not account(c,"S1_CONFLUENCE")["open_position"]
    assert c["simulated_entry_after_actual_receipt"]
    assert c["execution_authorized"] is False


def test_stop_first_when_both_target_and_stop_touched_in_entry_bar():
    score=ShadowForwardScorecard(friction=Friction(0,0,0))
    send(score,candle(0),proposed="LONG",filtered="WAIT")
    send(score,candle(1))
    result=send(score,candle(2,op=100,high=105,low=97))
    base=account(result,"STANDALONE")
    gated=account(result,"S1_CONFLUENCE")
    assert base["completed_trades"]==1
    assert base["stops"]==1 and base["targets"]==0
    assert base["net_pnl"]<0
    assert gated["completed_trades"]==0 and gated["net_pnl"]==0
    assert result["cash_benchmark_pnl"]==0


def test_slippage_commission_and_funding_reduce_hypothetical_profit():
    no_cost=ShadowForwardScorecard(friction=Friction(0,0,0))
    with_cost=ShadowForwardScorecard(friction=Friction(6,6,1))
    for score in (no_cost,with_cost):
        send(score,candle(0),proposed="LONG")
        send(score,candle(1))
    clean=send(no_cost,candle(2,op=100,high=104.5,low=99))
    costly=send(with_cost,candle(2,op=100,high=104.5,low=99))
    a=account(clean,"STANDALONE")
    b=account(costly,"STANDALONE")
    assert a["completed_trades"]==b["completed_trades"]==1
    assert a["targets"]==b["targets"]==1
    assert b["fees"]>0 and b["funding"]>0
    assert b["net_pnl"]<a["net_pnl"]


def test_duplicate_same_input_idempotent_and_conflicting_rejected():
    score=ShadowForwardScorecard()
    bar=candle(0)
    first=send(score,bar,proposed="LONG")
    second=send(score,bar,proposed="LONG")
    assert second["duplicate"] is True
    assert account(second,"STANDALONE")["pending"]
    assert len(score._events)==1
    with pytest.raises(ValueError,match="Conflicting"):
        send(score,bar,proposed="SHORT")


def test_missing_bar_halting_prevents_fake_pnl_or_reentry():
    score=ShadowForwardScorecard()
    send(score,candle(0),proposed="LONG")
    with pytest.raises(ValueError,match="Missing"):
        send(score,candle(2))
    with pytest.raises(ValueError,match="halted"):
        send(score,candle(3))
    assert score.snapshot("BTCUSDT")["status"]=="HALTED"


def test_late_signal_expires_instead_of_backdating_entry():
    score=ShadowForwardScorecard(friction=Friction(0,0,0))
    send(score,candle(0,received_offset=1000),proposed="LONG",offset=1000)
    # By the next bar observation, the next open was before the signal.
    send(score,candle(1,received_offset=1100),offset=1100)
    send(score,candle(2,received_offset=1200),offset=1200)
    outcome=send(score,candle(3,received_offset=5),offset=5)
    assert not account(outcome,"STANDALONE")["open_position"]
    assert account(outcome,"STANDALONE")["expired_signals"]==1


def test_contract_rejects_fake_order_permission_wrong_policy_and_wrong_symbol():
    b=candle(0)
    normal=shadow(b,proposed="LONG")
    score=ShadowForwardScorecard()
    for replacement in (
        {"execution_authorized":True},
        {"policy_commit":"different"},
        {"oms_touched":True},
        {"symbol":"SOLUSDT"},
    ):
        s={**normal,**replacement}
        with pytest.raises(ValueError,match="Untrusted"):
            score.observe(b,s,observed_at=b.received_at)
    bad={**normal,"hypotheses":[{**x,"filtered_action":"SHORT"}
                  if x["strategy_id"]==STRATEGY else x
                  for x in normal["hypotheses"]]}
    with pytest.raises(ValueError,match="Invalid S2E"):
        score.observe(b,bad,observed_at=b.received_at)


def test_unchanged_first_staged_candle_is_only_warmup_not_a_trade():
    before=candle(0).model_copy(update={
        "closed_at":datetime(2026,11,2,0,0,tzinfo=UTC),
        "received_at":datetime(2026,11,2,0,0,5,tzinfo=UTC)})
    score=ShadowForwardScorecard()
    result=send(score,before,proposed="LONG")
    assert result["accounts"]==[]
    assert not score._events


def test_symbol_scorebooks_are_independent_and_no_oms_activity():
    score=ShadowForwardScorecard()
    send(score,candle(0),proposed="LONG")
    eth=send(score,candle(0,symbol="ETHUSDT"),proposed="WAIT")
    assert account(eth,"STANDALONE")["completed_trades"]==0
    assert score.snapshot("BTCUSDT")["accounts"]
    assert eth["status"]=="RESEARCH_ONLY"
    assert eth["persistent_results"] is False
    assert eth["profitability_proven"] is False

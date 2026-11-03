"""Frozen lead parity, raw-period exclusions, failures, benchmarks and safeguards."""
import json
import math
import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime,timedelta,timezone

import pytest

from app.tests.test_gate_s4_slow_momentum import candles,update,FixtureReader
from app.strategy import gate_s4_sol_external as policy
from app.strategy import gate_s4_sol_external_study as study
from app.strategy.gate_s4_sol_external_replay import replay_external
from app.strategy.gate_s4_regime_signal import Ridge,RegimeReader
from app.strategy.gate_s2_replay import Friction


def sol(n=1500,trend=True):
    return [b.model_copy(update={"symbol":"SOLUSDT"}) for b in candles(n,trend=trend)]


@pytest.mark.parametrize("down",[False,True])
def test_frozen_candidate_matches_original_regime_long_control_rules(down):
    bars=sol()
    if down:bars=[update(b,10000/b.open,10000/b.low,10000/b.high,10000/b.close) for b in bars]
    model=Ridge((0.,)*6,(1.,)*6,(0.,)*7,0.,474,1194,"DUMMY_NOT_USED_BY_LONG_CONTROL")
    old=RegimeReader(bars,cap=1400,model=model);new=policy.ExternalReader(bars,cap=1400)
    for count in range(1234,1301):
        a=old.decision("REGIME_LONG_CONTROL",count,bars[count-1].closed_at)
        b=new.decision(policy.CANDIDATE,count,bars[count-1].closed_at)
        assert (a.action,a.reference_price,a.atr,a.stop_price,a.target_price,a.invalidation_level,a.setup_index)==(
            b.action,b.reference_price,b.atr,b.stop_price,b.target_price,b.invalidation_level,b.setup_index)
    d=new.decision(policy.CONTROL,1260,bars[1259].closed_at)
    assert d.action=="LONG" and not d.execution_authorized
    assert new.decision(policy.CANDIDATE,1260,bars[1259].closed_at).action==("WAIT" if down else "LONG")


def test_external_reader_rejects_other_market_future_and_stale_clocks():
    bars=sol(6500);reader=policy.ExternalReader(bars,cap=6000)
    for count,clock in [(6001,bars[6000].closed_at),(240,bars[238].closed_at),
                         (240,bars[239].closed_at+timedelta(hours=9))]:
        with pytest.raises(ValueError):reader.decision(policy.CANDIDATE,count,clock)
    with pytest.raises(ValueError):policy.ExternalReader(candles(300),cap=240)
    with pytest.raises(ValueError):policy.ExternalReader(bars,cap=6001)


class SolExecutionReader(FixtureReader):
    def decision(self,*args):return replace(super().decision(*args),symbol="SOLUSDT")


@pytest.mark.parametrize("arm",policy.ARMS)
def test_same_execution_oracle_stop_first_clocks_and_risk_for_both_arms(arm):
    bars=sol(260,trend=False);bars[223]=update(bars[223],100,135,65,100)
    value=replay_external(arm,bars,SolExecutionReader(),start=220,end=260,segment="FIXTURE",friction=Friction(0,0,0))
    t,=value["report"].trades
    assert (t.decision_bar_index,t.entry_bar_index,t.exit_bar_index)==(221,223,223)
    assert t.exit_reason=="STOP_FIRST" and t.net_pnl==pytest.approx(-2.5)
    with pytest.raises(ValueError,match="Untrusted"):
        replay_external(arm,bars,SolExecutionReader(short=True),start=220,end=260,segment="FIXTURE")


def test_buy_hold_flat_price_oracle_costs_funding_and_unmatched_risk_are_explicit():
    bars=sol(240,trend=False)
    clean=study.buy_hold_reference(bars,220,240,Friction(0,0,0),0)
    costly=study.buy_hold_reference(bars,220,240,Friction(),0)
    assert clean["net"]==0 and clean["quantity"]==10 and clean["candle_close_drawdown_pct"]==0
    assert costly["entry_index"]==222 and costly["exit_mark_index"]==239
    assert costly["funding_estimate"]==pytest.approx(.9)  # 72h, nine8h periods,1000 notional.
    assert costly["net"]==pytest.approx(costly["gross"]-costly["fees"]-.9)
    assert costly["net"]<0 and not costly["risk_exposure_matched_to_candidate"]
    assert not costly["risk_limits_or_liquidation_simulated"]


def test_daily_changes_sum_utc_close_days_without_averaging_or_compounding():
    bars=sol(6,trend=False)
    assert study.daily_changes(bars,[1,2,3,4,5,6],0,6)==[15,6]
    with pytest.raises(ValueError):study.daily_changes(bars,[1],0,6)
    with pytest.raises(ValueError):study.daily_changes(bars,[math.nan]*6,0,6)


def registered(tmp_path,monkeypatch):
    old=tmp_path/"selection.json";old.write_text("{}")
    discovery=tmp_path/"discovery.sqlite3";discovery.write_bytes(b"PARENT")
    rows=[{"symbol":s,"timeframe":t,"status":"AVAILABLE","first_closed_at":"2024-01-01T00:00:00+00:00"}
        for s in ("BTCUSDT","ETHUSDT","SOLUSDT") for t in ("15m","1h","4h")]
    monkeypatch.setattr(study,"catalog_rows",lambda _:rows)
    monkeypatch.setattr(study.discovery,"review",lambda _:{"complete":True,"evaluation_failed":0,"fit_failed":False,"journal_head":"DISCOVERY"})
    monkeypatch.setattr(study.discovery,"_path",lambda _:discovery)
    root=tmp_path/"study";m=study.register(root,old,tmp_path)
    return root,m,old


def test_registration_precedes_capture_and_locks_disjoint_dates_exclusive_no_retry(tmp_path,monkeypatch):
    root,m,old=registered(tmp_path,monkeypatch);calls=[]
    assert datetime.fromisoformat(m["external_last_expected_close_utc"])<datetime.fromisoformat(m["selection_earliest_close_utc"])
    def capture(*args,**kwargs):
        with study._connect(root,readonly=True) as db:assert [k for k,v in study._events(db)[0]]==["REGISTERED","STARTED"]
        calls.append(kwargs);return {"status":"UNAVAILABLE","reason":"MISSING_OLD_DATA"}
    monkeypatch.setattr(study,"capture_real_series",capture)
    r=study.run(root)
    assert len(calls)==1 and calls[0]["anchor"]==m["external_anchor_seconds"] and calls[0]["count"]==6000
    assert not r["complete"] and r["source"]["status"]=="UNAVAILABLE" and not r["financial_screen_passed"]
    assert all(t["status"]=="NOT_EVALUATED_FINANCIAL_GATE_FAILED" for t in r["statistical_diagnostics"])
    with pytest.raises(ValueError,match="already started"):study.run(root)
    with pytest.raises(FileExistsError):study.register(root,old,tmp_path)


@pytest.mark.parametrize("overlap",[False,True])
def test_external_boundaries_source_checks_and_two_windows_only(tmp_path,monkeypatch,overlap):
    root,m,_=registered(tmp_path,monkeypatch)
    first=datetime.fromisoformat(m["external_first_expected_close_utc"])
    bars=[b.model_copy(update={"closed_at":first+timedelta(hours=4*i),"received_at":datetime(2026,1,1,tzinfo=timezone.utc)}) for i,b in enumerate(sol(6000,trend=False))]
    if overlap:bars[-1]=bars[-1].model_copy(update={"closed_at":datetime.fromisoformat(m["selection_earliest_close_utc"])})
    folder=root/"history"/"SOLUSDT-4h";folder.mkdir(parents=True);(folder/"source.json").write_text("{}")
    monkeypatch.setattr(study,"capture_real_series",lambda *a,**k:{"status":"AVAILABLE","relative_path":"SOLUSDT-4h/candles.json","sha256":"FIXTURE"})
    monkeypatch.setattr(study,"load_real_dataset",lambda _:(bars,"FIXTURE"))
    monkeypatch.setattr(study,"verify_raw",lambda *a:{"raw_candles_verified":6000})
    calls=[]
    def evaluate(b,start,end):
        calls.append((start,end))
        metric={"completed_trades":0,"closed_net_pnl":0.,"net_including_boundary_mark":0.,
            "profit_factor":None,"largest_win_share":None,"candle_close_drawdown_pct":0.}
        return {"arms":{a:{s:dict(metric) for s in study.SCENARIOS} for a in policy.ARMS}}
    monkeypatch.setattr(study,"evaluate_fold",evaluate)
    monkeypatch.setattr(study.stats,"mean_diagnostic",lambda *a:pytest.fail("Financial gate failed: no statistics"))
    r=study.run(root)
    if overlap:
        assert not calls and not r["complete"] and r["source"]["error_type"]=="ValueError"
    else:
        assert calls==[(253,3000),(3033,6000)] and r["complete"] and not r["evaluation_failed"]
        assert not r["financial_screen_passed"] and not r["conditional_external_diagnostic_passed"]


def test_conditional_statistical_pass_still_cannot_promote_or_claim_profit(tmp_path,monkeypatch):
    root,_,_=registered(tmp_path,monkeypatch)
    with study._connect(root) as db,db:
        study.lab._append(db,"STARTED",{})
        study.lab._append(db,"SOURCE_AVAILABLE",{"status":"AVAILABLE"})
        for fold in (1,2):
            arm_values={}
            for arm in policy.ARMS:
                arm_values[arm]={n:{"completed_trades":30,"closed_net_pnl":10. if arm==policy.CANDIDATE else 5.,
                    "net_including_boundary_mark":10. if arm==policy.CANDIDATE else 5.,"profit_factor":1.3,
                    "largest_win_share":.2,"candle_close_drawdown_pct":1.,"daily_changes":[.0001]*400}
                    for n in study.SCENARIOS}
            study.lab._append(db,"RESULT",{"fold":fold,"arms":arm_values})
    monkeypatch.setattr(study.stats,"mean_diagnostic",lambda *a:{"status":"SYNTHETIC_ORACLE","marginal_p_value":.0001,
        "sensitivity":[{"marginal_p_value":.0001}]*3})
    r=study.review(root)
    assert r["financial_screen_passed"] and r["conditional_external_diagnostic_passed"]
    assert len(r["statistical_diagnostics"])==6 and all(t["sampling_unit"]=="UTC_DAY" for t in r["statistical_diagnostics"])
    assert all(not r[k] for k in ("profitability_proven","prospective_tracking_started","execution_authorized",
        "automatic_promotion_authorized","independent_prospective_evidence","existing_reserve_evaluated"))
    with study._connect(root) as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("DELETE FROM events")


def test_wrong_phase_and_modified_protocol_fail_closed(tmp_path,monkeypatch):
    root,_,_=registered(tmp_path,monkeypatch)
    with study._connect(root) as db,db:study.lab._append(db,"SOURCE_AVAILABLE",{})
    with pytest.raises(ValueError,match="start missing"):study.review(root)
    monkeypatch.setattr(study,"fingerprint",lambda:"CHANGED")
    with pytest.raises(ValueError,match="Frozen"):study.review(root)

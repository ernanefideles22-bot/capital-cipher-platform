"""Fit-only arithmetic, temporal boundaries, controls, shared execution and budgets."""
from dataclasses import replace
import hashlib
import math
import sqlite3
import pytest

from app.tests.test_gate_s4_slow_momentum import candles,FixtureReader,update
from app.strategy import gate_s4_regime_signal as signal
from app.strategy import gate_s4_regime_signal_study as study
from app.strategy.gate_s4_regime_signal_replay import replay_signal
from app.strategy.gate_s2_replay import Friction


def fit_rows():
    return [{"symbol":signal.SYMBOLS[i%3],"decision_index":220+6*i,"label_index":251+6*i,
        "x":[(-1.)**i,0.,0.,0.,0.,0.],"y":.01+.02*(-1.)**i} for i in range(120)]


def test_ridge_matches_closed_form_penalized_slope_and_unpenalized_intercept():
    model=signal.fit_ridge(fit_rows())
    assert model.coefficients[0]==pytest.approx(.01)
    assert model.coefficients[1]==pytest.approx(.02/1.1)
    assert model.coefficients[2:]==pytest.approx((0.,)*5)
    assert model.predict((1.,0,0,0,0,0))==pytest.approx(.01+.02/1.1)
    assert model.constant_forecast==pytest.approx(.01)
    assert model.means[0]==0 and model.scales[0]==1
    assert model.predict((1e100,0,0,0,0,0))==pytest.approx(.01+5*.02/1.1)


@pytest.mark.parametrize("change",["future_label","wrong_horizon","unknown_market","nonfinite","few_rows"])
def test_malformed_or_immature_fit_data_are_refused(change):
    rows=fit_rows()
    if change=="future_label":rows[-1].update(decision_index=1180,label_index=1211)
    elif change=="wrong_horizon":rows[0]["label_index"]+=1
    elif change=="unknown_market":rows[0]["symbol"]="OTHER"
    elif change=="nonfinite":rows[0]["x"][0]=math.nan
    else:rows=rows[:119]
    with pytest.raises(ValueError):signal.fit_ridge(rows)


def test_training_rows_stop_before_fit_end_and_ignore_every_later_price():
    bars=candles(1500)
    datasets={s:[b.model_copy(update={"symbol":s}) for b in bars] for s in signal.SYMBOLS}
    changed={s:b[:1200]+[update(v,10,11,9,10) for v in b[1200:]] for s,b in datasets.items()}
    a=signal.training_rows(datasets);b=signal.training_rows(changed)
    assert a==b and len(a)==474 and max(r["label_index"] for r in a)==1194
    assert all(r["label_index"]==r["decision_index"]+31 for r in a)
    assert signal.fit_ridge(a)==signal.fit_ridge(b)


def model(mu=.03,constant=.005):
    return signal.Ridge((0.,)*6,(1.,)*6,(mu,0.,0.,0.,0.,0.,0.),constant,474,1194,"FIXTURE")


@pytest.mark.parametrize("short",[False,True])
def test_frozen_model_regime_alignment_and_three_distinct_controls(short):
    bars=candles(1500)
    if short:bars=[update(b,10000/b.open,10000/b.low,10000/b.high,10000/b.close) for b in bars]
    reader=signal.RegimeReader(bars,cap=1400,model=model(-.03 if short else .03))
    d=reader.decision(signal.MODEL,1260,bars[1259].closed_at)
    assert d.action==("SHORT" if short else "LONG")
    assert abs(d.reference_price-d.stop_price)==pytest.approx(2*d.atr)
    assert abs(d.reference_price-d.target_price)==pytest.approx(6*d.atr)
    assert reader.decision("MEAN_FORECAST_CONTROL",1260,bars[1259].closed_at).action=="WAIT"
    assert reader.decision("MOMENTUM20_CONTROL",1260,bars[1259].closed_at).action==d.action
    assert reader.decision("REGIME_LONG_CONTROL",1260,bars[1259].closed_at).action==("WAIT" if short else "LONG")
    assert not d.execution_authorized and not d.predictive_edge_proven
    assert reader.decision(signal.MODEL,1261,bars[1260].closed_at).action=="WAIT"


def test_before_model_availability_future_clocks_and_reserved_prefixes_fail_closed():
    bars=candles(3700);reader=signal.RegimeReader(bars,cap=1400,model=model())
    for count,clock in [(1200,bars[1199].closed_at),(1260,bars[1258].closed_at),(1401,bars[1400].closed_at)]:
        with pytest.raises(ValueError):reader.decision(signal.MODEL,count,clock)
    with pytest.raises(ValueError):signal.RegimeReader(bars,cap=3601,model=model())
    too_late=signal.RegimeReader(bars,cap=1400,model=replace(model(),max_label_index=1300))
    with pytest.raises(ValueError):too_late.decision(signal.MODEL,1260,bars[1259].closed_at)


class ExecutionReader(FixtureReader):
    def decision(self,arm,count,clock):
        d=super().decision(arm,count,clock)
        return replace(d,action=("SHORT" if self.short else "LONG") if count==1242 else "WAIT",setup_index=count-1)


@pytest.mark.parametrize("arm",signal.ARMS)
@pytest.mark.parametrize("short",[False,True])
def test_identical_execution_risk_and_stop_first_for_all_four_arms(arm,short):
    bars=candles(1290,trend=False);bars[1243]=update(bars[1243],100,135,65,100)
    r=replay_signal(arm,bars,ExecutionReader(short),start=1240,end=1290,segment="FIXTURE",friction=Friction(0,0,0))
    t,=r["report"].trades
    assert (t.decision_bar_index,t.entry_bar_index,t.exit_bar_index)==(1241,1243,1243)
    assert t.strategy_id==arm and t.exit_reason=="STOP_FIRST" and t.net_pnl==pytest.approx(-2.5)
    assert not r["execution_authorized"] and not r["original_risk_policy_modified"]


def register_fixture(tmp_path,monkeypatch):
    catalog=tmp_path/"catalog.json";catalog.write_text("{}")
    rows=[]
    for symbol in signal.SYMBOLS:
        path=tmp_path/f"{symbol}-4h";path.mkdir();(path/"source.json").write_text("{}")
        rows.append({"symbol":symbol,"timeframe":"4h","status":"AVAILABLE","candles":6000,
            "relative_path":f"{symbol}-4h/candles.json","sha256":"FIXTURE",
            "source_archive_sha256":hashlib.sha256(b"{}").hexdigest()})
    monkeypatch.setattr(study,"catalog_rows",lambda _:rows)
    root=tmp_path/"study";study.register(root,catalog)
    return root,catalog


def test_one_fit_and_seventy_two_comparisons_only_in_predeclared_known_windows(tmp_path,monkeypatch):
    root,_=register_fixture(tmp_path,monkeypatch)
    bars=candles(6000)
    monkeypatch.setattr(study,"load_real_dataset",lambda p:([b.model_copy(update={"symbol":p.parent.name.split('-')[0]}) for b in bars],"FIXTURE"))
    monkeypatch.setattr(study,"training_rows",lambda _:fit_rows())
    monkeypatch.setattr(study,"forecast_diagnostics",lambda *args:{"rows":[]})
    calls=[]
    def measure(arm,bars,reader,start,end):
        calls.append((arm,start,end,reader.model.fit_rows_sha256))
        with study._connect(root,readonly=True) as db:assert any(k=="FIT" for k,v in study._events(db)[0])
        assert reader.cap==end<=3600 and reader.model.max_label_index<1200<start
        return {s:{"completed_trades":30,"closed_net_pnl":10. if arm==signal.MODEL else 5.,
            "net_including_boundary_mark":10. if arm==signal.MODEL else 5.,"profit_factor":1.3,
            "largest_win_share":.2,"candle_close_drawdown_pct":1.} for s in study.SCENARIOS}
    monkeypatch.setattr(study,"measure",measure)
    r=study.run(root)
    assert r["complete"] and r["research_screen_passed"]==3 and not r["evaluation_failed"]
    assert len(calls)==24 and len({c[-1] for c in calls})==1
    assert {(c[1],c[2]) for c in calls}=={(1233,2400),(2433,3600)}
    assert all(not r[k] for k in ("profitability_proven","independent_project_out_of_sample",
        "final_reserve_evaluated","prospective_tracking_started","automatic_promotion_authorized","execution_authorized"))
    with study._connect(root) as db:
        assert len(study._events(db)[0])==6
        with pytest.raises(sqlite3.IntegrityError):db.execute("DELETE FROM events")
    with pytest.raises(ValueError,match="already started"):study.run(root)


def test_failed_fit_is_preserved_without_retry_and_registration_is_exclusive(tmp_path,monkeypatch):
    root,catalog=register_fixture(tmp_path,monkeypatch)
    with pytest.raises(FileExistsError):study.register(root,catalog)
    monkeypatch.setattr(study,"load_real_dataset",lambda _:(_ for _ in ()).throw(ValueError("BAD SOURCE")))
    r=study.run(root)
    assert r["fit_failed"] and not r["complete"] and r["evaluation_cases"]==0
    with pytest.raises(ValueError,match="already started"):study.run(root)


def test_wrong_phase_and_modified_frozen_rules_are_refused(tmp_path,monkeypatch):
    root,_=register_fixture(tmp_path,monkeypatch)
    with study._connect(root) as db,db:study.lab._append(db,"FIT",{})
    with pytest.raises(ValueError,match="start missing"):study.review(root)
    monkeypatch.setattr(study,"fingerprint",lambda:"CHANGED")
    with pytest.raises(ValueError,match="Frozen"):study.review(root)

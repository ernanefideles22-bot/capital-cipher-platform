"""Independent raw field, outcome horizon, cost and registry oracles."""
import copy
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from app.tests.test_gate_s4_slow_momentum import candles, FixtureReader
from app.strategy import gate_s4_momentum_diagnostics as diagnostic
from app.strategy.gate_s4_slow_momentum import POLICY_ID


def archive(bars):
    rows=[{"t":int(b.closed_at.timestamp())-14400,"o":str(b.open),"h":str(b.high),
        "l":str(b.low),"c":str(b.close),"v":str(b.volume*10000)} for b in bars]
    def page(path,value):
        raw=json.dumps(value)
        return {"url":diagnostic.ORIGIN+"/api/v4/futures/usdt/"+path,
            "raw_response_utf8":raw,"response_sha256":hashlib.sha256(raw.encode()).hexdigest(),
            "received_at_utc":bars[-1].received_at.isoformat()}
    return {"raw_pages":[page("contracts/BTC_USDT",{"name":"BTC_USDT","quanto_multiplier":".0001"}),
        page("candlesticks?contract=BTC_USDT&interval=4h",rows)]}


def raw_fixture():
    bars=candles(240,trend=False)
    receipt=bars[-1].received_at
    bars=[b.model_copy(update={"received_at":receipt}) for b in bars]
    return bars,archive(bars)


def test_independent_raw_mapping_matches_ohlcv_start_to_close_and_receipts():
    bars,a=raw_fixture()
    result=diagnostic.verify_raw(bars,a)
    assert result["raw_candles_verified"]==240 and result["raw_pages_verified"]==2
    assert result["ohlc_volume_timestamps_and_receipts_match"]


@pytest.mark.parametrize("problem",["price","volume","timestamp","receipt","duplicate","origin","contract","hash"])
def test_raw_mapping_detects_each_corruption_even_with_rehashed_payload(problem):
    bars,a=raw_fixture();a=copy.deepcopy(a)
    if problem in ("price","volume","timestamp","duplicate"):
        p=a["raw_pages"][1];rows=json.loads(p["raw_response_utf8"])
        if problem=="price":rows[0]["c"]="999"
        elif problem=="volume":rows[0]["v"]="10"
        elif problem=="timestamp":rows[0]["t"]+=14400
        else:rows.append(rows[0])
        p["raw_response_utf8"]=json.dumps(rows);p["response_sha256"]=hashlib.sha256(p["raw_response_utf8"].encode()).hexdigest()
    elif problem=="receipt":a["raw_pages"][1]["received_at_utc"]=(bars[-1].received_at+timedelta(seconds=1)).isoformat()
    elif problem=="origin":a["raw_pages"][1]["url"]=a["raw_pages"][1]["url"].replace("api.gateio.ws","evil.invalid")
    elif problem=="contract":a["raw_pages"][1]["url"]=a["raw_pages"][1]["url"].replace("BTC_USDT","ETH_USDT")
    else:a["raw_pages"][1]["response_sha256"]="BAD"
    with pytest.raises((ValueError,KeyError)):diagnostic.verify_raw(bars,a)


class EveryProposal(FixtureReader):
    def decision(self,policy,count,clock):
        return replace(super().decision(policy,count,clock),action="LONG",setup_index=count-1)


def test_all_horizons_have_full_training_coverage_and_nonoverlap_is_frozen_greedy():
    bars=candles(300,trend=False)
    rows,excluded=diagnostic.event_rows(bars,EveryProposal(),[],start=220,end=300)
    assert excluded==31 and len(rows)==49
    assert rows[0]["entry_index"]==222 and rows[-1]["marks"]["30"]["last_index"]==299
    selected=[r for r in rows if r["nonoverlapping"]]
    assert [r["entry_index"] for r in selected]==[222,252]
    assert len(diagnostic.summaries(rows))==9
    assert all(not r["tradable_pnl_estimate"] for r in diagnostic.summaries(rows))


def test_horizon_mark_uses_later_open_and_exact_horizon_close_with_independent_bps():
    bars=candles(280,trend=False)
    # Decision at 221, entry 223, 6-bar outcome at 228. No last-bar off-by-one.
    bars[223]=bars[223].model_copy(update={"open":100.,"high":101.,"low":99.})
    bars[228]=bars[228].model_copy(update={"close":110.,"high":111.})
    rows,_=diagnostic.event_rows(bars,FixtureReader(),[],start=220,end=280)
    assert len(rows)==1 and rows[0]["entry_index"]==223
    assert rows[0]["marks"]["6"]["last_index"]==228
    assert rows[0]["marks"]["6"]["signed_price_bps"]==pytest.approx(1000.)
    short,_=diagnostic.event_rows(bars,FixtureReader(short=True),[],start=220,end=280)
    assert short[0]["marks"]["6"]["signed_price_bps"]==pytest.approx(-1000.)


def test_outside_train_tail_cannot_change_a_summary_and_actual_clock_must_match():
    bars=candles(330,trend=False)
    changed=bars[:300]+[b.model_copy(update={"close":10000.}) for b in bars[300:]]
    a,_=diagnostic.event_rows(bars,EveryProposal(),[],start=220,end=300)
    b,_=diagnostic.event_rows(changed,EveryProposal(),[],start=220,end=300)
    assert diagnostic.summaries(a)==diagnostic.summaries(b)
    with pytest.raises(ValueError,match="clock"):
        diagnostic.event_rows(bars,EveryProposal(),[{"decision_bar_index":220,"entry_bar_index":221,"side":"LONG"}],start=220,end=300)


def test_cost_decomposition_and_excursion_exclude_unknown_exit_candle_path():
    bars=candles(280,trend=False)
    bars[223]=bars[223].model_copy(update={"high":105.,"low":95.})
    bars[224]=bars[224].model_copy(update={"high":1000.,"low":1.})
    entry=100*1.0006;exit=90*.9994;gross=(exit-entry)*.25;fees=(entry+exit)*.25*.0006
    t={"strategy_id":POLICY_ID,"decision_bar_index":221,"entry_bar_index":223,"exit_bar_index":224,
        "side":"LONG","entry_price":entry,"exit_price":exit,"stop_price":90.,"quantity":.25,
        "gross_pnl":gross,"fees_paid":fees,"funding_paid":.01,"net_pnl":gross-fees-.01,"exit_reason":"STOP"}
    r=diagnostic.trade_paths(bars,[t],end=280);row=r["records"][0]
    assert row["raw_gross_on_executed_path"]==pytest.approx(-2.5)
    assert row["completed_bar_favorable_excursion_r_before_exit"]==pytest.approx((105-entry)/(entry-90))
    assert row["exit_bar_high_low_excluded"] and not row["exact_intrabar_excursion_known"]
    assert not r["zero_cost_counterfactual_replay"] and not r["exit_parameters_optimized"]


def fake_parent(tmp_path,monkeypatch):
    root=tmp_path/"parent";root.mkdir();db=root/"parent.sqlite3";db.write_bytes(b"PARENT")
    r={"complete":True,"training_failed":0,"journal_head":"HEAD","results":[{}]*3,
        "manifest":{"datasets":[{"symbol":s,"timeframe":"4h","relative_path":f"{s}-4h/candles.json"}
            for s in diagnostic.PROTOCOL["markets"]],"catalog_path":str(tmp_path/"catalog.json")}}
    monkeypatch.setattr(diagnostic.parent,"review",lambda _:r)
    monkeypatch.setattr(diagnostic.parent,"_path",lambda _:db)
    return root,db


def test_registration_phase_no_retry_changed_parent_and_append_only(tmp_path,monkeypatch):
    origin,parent_db=fake_parent(tmp_path,monkeypatch)
    root=tmp_path/"audit";diagnostic.register(root,origin)
    with pytest.raises(FileExistsError):diagnostic.register(root,origin)
    parent_db.write_bytes(b"CHANGED")
    with pytest.raises(ValueError,match="Parent evidence changed"):diagnostic.run(root)
    parent_db.write_bytes(b"PARENT")
    # Missing source must preserve a bounded failure after STARTED, never a retry.
    result=diagnostic.run(root)
    assert result["status"]=="FAILED" and result["result"]["error_type"]=="FileNotFoundError"
    with pytest.raises(ValueError,match="already started"):diagnostic.run(root)
    with diagnostic._connect(root) as db:
        with pytest.raises(sqlite3.IntegrityError,match="append-only"):db.execute("DELETE FROM events")


def test_registered_protocol_change_and_wrong_phase_fail_closed(tmp_path,monkeypatch):
    origin,_=fake_parent(tmp_path,monkeypatch);root=tmp_path/"audit";diagnostic.register(root,origin)
    with diagnostic._connect(root) as db,db:diagnostic.lab._append(db,"RESULT",{})
    with pytest.raises(ValueError,match="start missing"):diagnostic.review(root)
    monkeypatch.setattr(diagnostic,"fingerprint",lambda:"CHANGED")
    with pytest.raises(ValueError,match="Frozen"):diagnostic.review(root)

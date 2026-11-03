"""Only the documented API retention failure may change the data-window length."""
import json
from datetime import datetime,timedelta,timezone
import pytest

from app.tests.test_gate_s4_sol_external import registered,sol
from app.strategy import gate_s4_sol_external_study as v1
from app.strategy import gate_s4_sol_external_window_v2 as v2
from app.strategy.gate_s4_sol_external import ARMS


def setup(tmp_path,monkeypatch):
    old,m,catalog=registered(tmp_path,monkeypatch)
    monkeypatch.setattr(v1,"capture_real_series",lambda *a,**k:{"status":"UNAVAILABLE","reason":"HTTP400"})
    v1.run(old)
    (old.parent/"source-error-diagnostic.json").write_text(json.dumps({"http_status":400,"response_utf8":"Maximum10000 points recently are allowed"}))
    monkeypatch.setattr(v2,"catalog_rows",v1.catalog_rows)
    root=tmp_path/"v2";new=v2.register(root,catalog,tmp_path,old)
    return old,m,root,new


def test_retention_adaptation_preserves_failed_journal_end_date_and_no_strategy_change(tmp_path,monkeypatch):
    old,m,root,new=setup(tmp_path,monkeypatch)
    assert new["external_candles"]==3600 and m["external_candles"]==6000
    assert new["external_anchor_seconds"]==m["external_anchor_seconds"]
    assert new["external_first_expected_close_utc"]>m["external_first_expected_close_utc"]
    assert new["prior_source_failure_head"]==v1.review(old)["journal_head"]
    assert new["hypothesis"]["strategy_rule_version_unchanged"] and new["parameter_grid_budget"]==0
    calls=[]
    monkeypatch.setattr(v2,"capture_real_series",lambda *a,**k:calls.append(k) or {"status":"UNAVAILABLE"})
    r=v2.run(root)
    assert len(calls)==1 and calls[0]["count"]==3600 and not r["complete"]
    with pytest.raises(ValueError,match="already started"):v2.run(root)


def test_nonretention_error_cannot_authorize_another_data_window(tmp_path,monkeypatch):
    old,m,root,new=setup(tmp_path,monkeypatch)
    (old.parent/"source-error-diagnostic.json").write_text(json.dumps({"http_status":503,"response_utf8":"OTHER"}))
    with pytest.raises(ValueError,match="retention failure"):v2.register(tmp_path/"blocked",tmp_path/"selection.json",tmp_path,old)


def test_available_retention_window_uses_only_two_fixed_blocks(tmp_path,monkeypatch):
    old,m,root,new=setup(tmp_path,monkeypatch)
    first=datetime.fromisoformat(new["external_first_expected_close_utc"])
    bars=[b.model_copy(update={"closed_at":first+timedelta(hours=4*i),"received_at":datetime(2026,1,1,tzinfo=timezone.utc)})
        for i,b in enumerate(sol(3600,trend=False))]
    folder=root/"history"/"SOLUSDT-4h";folder.mkdir(parents=True);(folder/"source.json").write_text("{}")
    monkeypatch.setattr(v2,"capture_real_series",lambda *a,**k:{"status":"AVAILABLE","relative_path":"SOLUSDT-4h/candles.json","sha256":"FIXTURE"})
    monkeypatch.setattr(v2,"load_real_dataset",lambda _:(bars,"FIXTURE"))
    monkeypatch.setattr(v2,"verify_raw",lambda *a:{"raw_candles_verified":3600})
    calls=[]
    def evaluate(b,start,end):
        calls.append((start,end))
        metric={"completed_trades":0,"closed_net_pnl":0.,"net_including_boundary_mark":0.,
            "profit_factor":None,"largest_win_share":None,"candle_close_drawdown_pct":0.}
        return {"arms":{a:{s:dict(metric) for s in v2.SCENARIOS} for a in ARMS}}
    monkeypatch.setattr(v2,"evaluate_fold",evaluate)
    r=v2.run(root)
    assert r["complete"] and calls==[(253,1800),(1833,3600)]
    assert not r["financial_screen_passed"] and not r["existing_reserve_evaluated"]
    assert v1.review(old)["folds"]==[]

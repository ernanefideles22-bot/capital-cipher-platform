"""One pre-data registered, backward historical challenge of the SOL lead."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_regime_signal_study as discovery
from app.strategy import gate_s4_statistics as stats
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2_replay import ResearchRisk,_adverse_execution
from app.strategy.gate_s4_momentum_diagnostics import verify_raw
from app.strategy.gate_s4_playbook_study import CRITERIA,SCENARIOS,gate,monte_carlo_upper
from app.strategy.gate_s4_real_source import capture_real_series,load_real_dataset
from app.strategy.gate_s4_real_study import catalog_rows
from app.strategy.gate_s4_sol_external import ARMS,CANDIDATE,CONTROL,WINDOWS,ExternalReader,manifest
from app.strategy.gate_s4_sol_external_replay import replay_external
from app.strategy.gate_s4_validation import equity_changes

ENGINE="S4_SOL_FROZEN_EXTERNAL_HISTORY_CHALLENGE_V1"
FAMILY=6


def fingerprint():
    names=("gate_s4_sol_external.py","gate_s4_sol_external_replay.py","gate_s4_sol_external_study.py",
        "gate_s4_momentum_diagnostics.py","gate_s4_statistics.py","gate_s4_playbook_study.py")
    return lab.digest({"discovery":discovery.fingerprint(),**{
        n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}})


def _path(root):
    p=lab._db_path(root).with_name("s4-sol-external.sqlite3")
    if p.is_symlink():raise ValueError("Journal symlink refused")
    return p


def _connect(root,readonly=False):
    db=sqlite3.connect(_path(root).as_uri()+("?mode=ro" if readonly else "?mode=rw"),uri=True)
    if readonly:db.execute("PRAGMA query_only=ON")
    return db


def _events(db):
    out,previous=[],lab.GENESIS
    for expected,(seq,kind,payload,prev,sha) in enumerate(db.execute(
            "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"),1):
        if expected>5 or seq!=expected or prev!=previous or sha!=lab.digest([prev,kind,payload]):
            raise ValueError("External journal integrity/budget failure")
        out.append((kind,json.loads(payload)));previous=sha
    if not out or out[0][0]!="REGISTERED":raise ValueError("Registration missing")
    m=out[0][1]
    if (m["engine"]!=ENGINE or m["engine_sha256"]!=fingerprint() or m["hypothesis"]!=manifest()
            or m["criteria"]!=CRITERIA or m["statistical_family_slots"]!=FAMILY):
        raise ValueError("Frozen external rules/source changed")
    seen=set()
    for i,(kind,v) in enumerate(out[1:],1):
        if i==1:
            if kind!="STARTED":raise ValueError("External start missing")
        elif i==2:
            if kind not in ("SOURCE_AVAILABLE","SOURCE_UNAVAILABLE"):raise ValueError("Source phase missing")
        elif (out[2][0]!="SOURCE_AVAILABLE" or kind not in ("RESULT","FAILED") or
                v["fold"] not in (1,2) or v["fold"] in seen):raise ValueError("Unexpected/repeated external evaluation")
        else:seen.add(v["fold"])
    return out,previous


def _append(root,kind,value):
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");_events(db);lab._append(db,kind,value)


def register(root,catalog_path,discovery_root):
    rows=catalog_rows(catalog_path);prior=discovery.review(discovery_root)
    if not prior["complete"] or prior["evaluation_failed"] or prior["fit_failed"]:
        raise ValueError("Completed discovery evidence required")
    if any(r["status"]!="AVAILABLE" for r in rows):raise ValueError("Full selection-source exclusion map required")
    first=min(datetime.fromisoformat(r["first_closed_at"]) for r in rows)
    anchor=int((first-timedelta(hours=4)).timestamp())//14400*14400
    m={"engine":ENGINE,"engine_sha256":fingerprint(),"hypothesis":manifest(),"criteria":CRITERIA,
        "registered_at_utc":datetime.now(timezone.utc).isoformat(),"discovery_journal_head":prior["journal_head"],
        "discovery_db_sha256":hashlib.sha256(discovery._path(discovery_root).read_bytes()).hexdigest(),
        "selection_catalog_sha256":hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest(),
        "selection_sources":rows,"selection_earliest_close_utc":first.isoformat(),
        "external_anchor_seconds":anchor,"external_candles":6000,"source_capture_budget":1,
        "external_first_expected_close_utc":datetime.fromtimestamp(anchor-5999*14400,timezone.utc).isoformat(),
        "external_last_expected_close_utc":datetime.fromtimestamp(anchor,timezone.utc).isoformat(),
        "candidate_trials":1,"candidate_replays":6,"matched_control_replays":6,"buy_hold_references":6,
        "statistical_family_slots":FAMILY,"statistics":{"replications":stats.REPLICATIONS,
            "blocks_days":list(stats.BLOCK_LENGTHS),"seed":stats.SEED,"alpha":stats.ALPHA,
            "monte_carlo_family_error_budget":.05},"risk":asdict(ResearchRisk()),
        "scenarios":{n:{"friction":asdict(f),"extra_entry_delay":d} for n,(f,d) in SCENARIOS.items()},
        "backward_historical_challenge":True,"independent_prospective_evidence":False,
        "all_project_unseen_history_certified":False,"prior_project_searches_fully_corrected":False,
        "existing_reserve_access_authorized":False,"parameter_grid_budget":0,
        "execution_authorized":False,"automatic_promotion_authorized":False}
    p=_path(root);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("xb"):pass
    with closing(_connect(root)) as db,db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        lab._append(db,"REGISTERED",m)
    return m


def daily_changes(bars,changes,start,end):
    if len(changes)!=end-start:raise ValueError("Equity clock length mismatch")
    days={}
    for b,value in zip(bars[start:end],changes):
        if not math.isfinite(value):raise ValueError("Nonfinite daily return")
        key=b.closed_at.astimezone(timezone.utc).date().isoformat()
        days[key]=days.get(key,0.)+value
    return list(days.values())


def buy_hold_reference(bars,start,end,friction,delay):
    """1x fully funded perpetual reference, with unmatched risk/exposure disclosed."""
    entered=start+2+delay;entry=_adverse_execution(bars[entered].open,1,opening=True,friction=friction)
    quantity=1000./entry;peak=1000.;dd=0.;last=None
    for j in range(entered,end):
        exit_price=_adverse_execution(bars[j].close,1,opening=False,friction=friction)
        fees=(entry+exit_price)*quantity*friction.fee_bps_side/10000
        elapsed=(bars[j].closed_at-bars[entered-1].closed_at).total_seconds()
        funding=entry*quantity*friction.funding_bps_8h/10000*max(1,math.ceil(elapsed/28800))
        gross=(exit_price-entry)*quantity;net=gross-fees-funding;peak=max(peak,1000.+net)
        dd=max(dd,(1-(1000.+net)/peak)*100)
        last={"entry_index":entered,"exit_mark_index":j,"entry_price":entry,"exit_mark_price":exit_price,
            "quantity":quantity,"gross":gross,"fees":fees,"funding_estimate":funding,"net":net,
            "candle_close_drawdown_pct":dd,"risk_limits_or_liquidation_simulated":False,
            "risk_exposure_matched_to_candidate":False,"funding_always_debit_assumption":True,
            "is_perpetual_reference_not_spot_pnl":True,"tradable_execution_proven":False}
    return last


def evaluate_fold(bars,start,end):
    reader=ExternalReader(bars[:end],cap=end);arms={}
    for arm in ARMS:
        results={}
        for name,(friction,delay) in SCENARIOS.items():
            value=replay_external(arm,bars[:end],reader,start=start,end=end,segment="EXTERNAL_PAST_HISTORY",
                friction=friction,risk=ResearchRisk(),extra_delay=delay)
            report=value["report"];equity=equity_changes(report,bars[:end],start=start,end=end,friction=friction)
            results[name]={**lab.trade_metrics(report),**equity,
                "daily_changes":daily_changes(bars,equity["changes_over_initial_equity"],start,end),
                "attribution":asdict(attribute(report,assumptions=friction)),"trades":[asdict(t) for t in report.trades],
                **{k:v for k,v in value.items() if k!="report"}}
        arms[arm]=results
    return {"window":[start,end],"arms":arms,"buy_hold_reference":{
        n:buy_hold_reference(bars,start,end,f,d) for n,(f,d) in SCENARIOS.items()}}


def run(root):
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");events,_=_events(db)
        if len(events)!=1:raise ValueError("External study already started; no retry/reset")
        m=events[0][1];lab._append(db,"STARTED",{"at_utc":datetime.now(timezone.utc).isoformat()})
    try:
        source=capture_real_series(Path(root)/"history","SOLUSDT","4h",anchor=m["external_anchor_seconds"],count=6000)
        if source["status"]!="AVAILABLE":
            _append(root,"SOURCE_UNAVAILABLE",source);return review(root)
        path=Path(root)/"history"/source["relative_path"];bars,sha=load_real_dataset(path)
        archive_bytes=path.with_name("source.json").read_bytes();archive=json.loads(archive_bytes)
        if (sha!=source["sha256"] or len(bars)!=6000 or
            bars[0].closed_at.isoformat()!=m["external_first_expected_close_utc"] or
            bars[-1].closed_at.isoformat()!=m["external_last_expected_close_utc"] or
            bars[-1].closed_at>=datetime.fromisoformat(m["selection_earliest_close_utc"])):
            raise ValueError("External period/data overlap or range mismatch")
        provenance=verify_raw(bars,archive)
    except Exception as exc:
        _append(root,"SOURCE_UNAVAILABLE",{"status":"UNAVAILABLE","error_type":type(exc).__name__,
            "error_code":"REGISTERED_EXTERNAL_SOURCE_UNAVAILABLE"});return review(root)
    _append(root,"SOURCE_AVAILABLE",{**source,"source_archive_sha256":hashlib.sha256(archive_bytes).hexdigest(),
        "independent_raw_mapping":provenance,"selection_archive_nonoverlap_verified":True})
    for fold,(start,end) in enumerate(WINDOWS,1):
        try:value,kind={"fold":fold,**evaluate_fold(bars,start,end)},"RESULT"
        except Exception as exc:value,kind={"fold":fold,"error_type":type(exc).__name__,"error_code":"EXTERNAL_EVALUATION_UNAVAILABLE"},"FAILED"
        _append(root,kind,value);print(lab.canonical({"fold":fold,"status":kind}),flush=True)
    return review(root)


def review(root):
    with closing(_connect(root,readonly=True)) as db:
        db.execute("BEGIN");events,head=_events(db)
    source=next((v for k,v in events if k.startswith("SOURCE_")),None)
    folds=[v for k,v in events if k in ("RESULT","FAILED")];complete=len(folds)==2
    reasons=[]
    if not complete:reasons.append("EXTERNAL_EVIDENCE_INCOMPLETE")
    for f in folds:
        if "error_code" in f:reasons.append(f["error_code"]);continue
        reasons.extend(f"FOLD_{f['fold']}_"+x for x in gate(f["arms"][CANDIDATE]))
        for n in SCENARIOS:
            if f["arms"][CANDIDATE][n]["net_including_boundary_mark"]<=f["arms"][CONTROL][n]["net_including_boundary_mark"]:
                reasons.append(f"FOLD_{f['fold']}_{n}_NOT_ABOVE_MATCHED_EXIT_CONTROL")
    tests=[];delta=.05/(FAMILY*len(stats.BLOCK_LENGTHS))
    for scenario in SCENARIOS:
        for benchmark in ("CASH",CONTROL):
            if reasons:
                diagnostic,precision={"status":"NOT_EVALUATED_FINANCIAL_GATE_FAILED","marginal_p_value":1.},1.
            else:
                values=[]
                for f in folds:
                    a=f["arms"][CANDIDATE][scenario]["daily_changes"]
                    b=[0.]*len(a) if benchmark=="CASH" else f["arms"][CONTROL][scenario]["daily_changes"]
                    if len(a)!=len(b):raise ValueError("Control/candidate daily clocks differ")
                    values.append([x-y for x,y in zip(a,b)])
                diagnostic=stats.mean_diagnostic(values)
                precision=max((monte_carlo_upper(round(s["marginal_p_value"]*(stats.REPLICATIONS+1)-1),
                    stats.REPLICATIONS,delta) for s in diagnostic.get("sensitivity",[])),default=1.)
            tests.append({"scenario":scenario,"benchmark":benchmark,"sampling_unit":"UTC_DAY",
                **diagnostic,"mc_upper_probability":precision})
    point=stats.holm_adjust([t["marginal_p_value"] for t in tests]);upper=stats.holm_adjust([t["mc_upper_probability"] for t in tests])
    for t,p,u in zip(tests,point,upper):t.update(holm_point_p=p,holm_mc_upper_p=u)
    return {"engine":ENGINE,"manifest":events[0][1],"journal_head":head,"source":source,
        "complete":complete,"evaluation_failed":sum("error_code" in f for f in folds),"folds":folds,
        "financial_screen_reasons":reasons,"financial_screen_passed":not reasons,
        "statistical_diagnostics":tests,"conditional_external_diagnostic_passed":not reasons and all(u<=.05 for u in upper),
        "stationarity_assumptions_independently_verified":False,"independent_prospective_evidence":False,
        "existing_reserve_evaluated":False,"profitability_proven":False,"prospective_tracking_started":False,
        "execution_authorized":False,"automatic_promotion_authorized":False}

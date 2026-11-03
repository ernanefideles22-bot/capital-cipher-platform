"""One predeclared model fit, two purged temporal blocks and three controls."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
from statistics import fmean

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_slow_momentum_study as legacy
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2_replay import ResearchRisk
from app.strategy.gate_s4_playbook_study import CRITERIA,SCENARIOS,gate
from app.strategy.gate_s4_real_study import catalog_rows
from app.strategy.gate_s4_real_source import load_real_dataset
from app.strategy.gate_s4_regime_signal import (
    MODEL,ARMS,RULES,SYMBOLS,MomentumReader,RegimeReader,features,fit_ridge,training_rows,manifest,
)
from app.strategy.gate_s4_regime_signal_replay import replay_signal
from app.strategy.gate_s4_validation import equity_changes

ENGINE="S4_ONE_POOLED_REGIME_FORECAST_PURGED_BLOCKS_V1"


def fingerprint():
    names=("gate_s4_regime_signal.py","gate_s4_regime_signal_replay.py","gate_s4_regime_signal_study.py")
    return lab.digest({"base":legacy.fingerprint(),**{n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}})


def _path(root):
    p=lab._db_path(root).with_name("s4-regime-signal.sqlite3")
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
        if expected>6 or seq!=expected or prev!=previous or sha!=lab.digest([prev,kind,payload]):
            raise ValueError("Journal integrity/budget failure")
        out.append((kind,json.loads(payload)));previous=sha
    if not out or out[0][0]!="REGISTERED":raise ValueError("Registration missing")
    m=out[0][1]
    if (m["engine"]!=ENGINE or m["engine_sha256"]!=fingerprint() or m["signal"]!=manifest()
        or m["criteria"]!=CRITERIA or m["replay_budget"]!=72):raise ValueError("Frozen model/criteria/source changed")
    seen=set()
    for i,(kind,v) in enumerate(out[1:],1):
        if i==1:
            if kind!="STARTED":raise ValueError("Study start missing")
        elif i==2:
            if kind not in ("FIT","FIT_FAILED"):raise ValueError("Model fit missing")
            if kind=="FIT" and (v["model_sha256"]!=lab.digest(v["model"]) or v["model"]["max_label_index"]>=1200):
                raise ValueError("Changed model or labels beyond fit boundary")
        elif (out[2][0]!="FIT" or kind not in ("RESULT","FAILED") or v["symbol"] not in SYMBOLS or v["symbol"] in seen):
            raise ValueError("Unexpected/repeated evaluation")
        else:seen.add(v["symbol"])
    return out,previous


def _append(root,kind,value):
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");_events(db);lab._append(db,kind,value)


def register(root,catalog_path):
    rows=[r for r in catalog_rows(catalog_path) if r["timeframe"]=="4h"]
    if (len(rows)!=3 or {r["symbol"] for r in rows}!=set(SYMBOLS) or
            any(r["status"]!="AVAILABLE" or r["candles"]!=6000 for r in rows)):
        raise ValueError("Three complete 6000-bar production-public 4h sources required")
    m={"engine":ENGINE,"engine_sha256":fingerprint(),"registered_at_utc":datetime.now(timezone.utc).isoformat(),
        "signal":manifest(),"criteria":CRITERIA,"datasets":rows,"catalog_path":str(Path(catalog_path).resolve()),
        "catalog_sha256":hashlib.sha256(Path(catalog_path).read_bytes()).hexdigest(),
        "fit_window":[220,1200],"evaluation_windows":RULES["evaluation_windows"],
        "pooled_model_fits":1,"hyperparameter_grid_budget":0,"candidate_market_cases":3,
        "replay_budget":72,"candidate_replays":18,"control_replays":54,
        "scenarios":{s:{"friction":asdict(f),"extra_entry_delay":d} for s,(f,d) in SCENARIOS.items()},
        "risk":asdict(ResearchRisk()),"project_history_previously_inspected":True,
        "temporal_model_fit_separation_is_independent_project_oos":False,
        "statistical_significance_tested":False,"final_reserve_authorized":False,
        "automatic_promotion_authorized":False,"execution_authorized":False}
    path=_path(root);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("xb"):pass
    with closing(_connect(root)) as db,db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        lab._append(db,"REGISTERED",m)
    return m


def forecast_diagnostics(bars,reader,model,start,end):
    rows=[]
    for i in range(start,end-31):
        stamp=bars[i].closed_at.astimezone(timezone.utc)
        if (stamp.hour,stamp.minute,stamp.second,stamp.microsecond)!=(0,0,0,0):continue
        x=features(reader.base,i+1)
        if x is None:continue
        y=bars[i+31].close/bars[i+2].open-1;mu=model.predict(x)
        rows.append({"decision_index":i,"last_label_index":i+31,"prediction":mu,"outcome":y,
            "regime":"UP" if reader.momentum(i+1)>0 else "DOWN_OR_FLAT",
            "short_volatility_regime":"HIGH" if x[3]>0 else "LOW"})
    if not rows:return {"rows":[],"mse":None,"mse_constant":None,"skill_vs_constant":None}
    mse=fmean((r["prediction"]-r["outcome"])**2 for r in rows)
    constant=fmean((model.constant_forecast-r["outcome"])**2 for r in rows)
    groups={}
    for key in ("UP","DOWN_OR_FLAT","HIGH","LOW"):
        subset=[r for r in rows if key in (r["regime"],r["short_volatility_regime"])]
        groups[key]={"observations":len(subset),"mse":fmean((r["prediction"]-r["outcome"])**2 for r in subset) if subset else None}
    return {"rows":rows,"mse":mse,"mse_constant":constant,
        "skill_vs_constant":1-mse/constant if constant>0 else None,"regimes":groups,
        "overlapping_labels":True,"independent_samples_established":False,"tradable_pnl":False}


def measure(arm,bars,reader,start,end):
    output={}
    for scenario,(friction,delay) in SCENARIOS.items():
        value=replay_signal(arm,bars[:end],reader,start=start,end=end,segment="PURGED_KNOWN_HISTORY_BLOCK",
            friction=friction,risk=ResearchRisk(),extra_delay=delay)
        report=value["report"]
        output[scenario]={**lab.trade_metrics(report),
            **equity_changes(report,bars[:end],start=start,end=end,friction=friction),
            "attribution":asdict(attribute(report,assumptions=friction)),"trades":[asdict(t) for t in report.trades],
            **{k:v for k,v in value.items() if k!="report"}}
    return output


def run(root,source_root=None):
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");events,_=_events(db)
        if len(events)!=1:raise ValueError("Study already started; no retry/reset")
        m=events[0][1];lab._append(db,"STARTED",{"at_utc":datetime.now(timezone.utc).isoformat()})
    try:
        source=Path(source_root) if source_root else Path(m["catalog_path"]).parent
        datasets={}
        for row in m["datasets"]:
            path=source/row["relative_path"];bars,sha=load_real_dataset(path)
            if sha!=row["sha256"] or hashlib.sha256(path.with_name("source.json").read_bytes()).hexdigest()!=row["source_archive_sha256"]:
                raise ValueError("Registered data/source changed")
            datasets[row["symbol"]]=bars
        if len({b[0].closed_at for b in datasets.values()})!=1:
            raise ValueError("Pooled market clocks are not aligned")
        rows=training_rows(datasets);model=fit_ridge(rows);serialized=asdict(model)
        _append(root,"FIT",{"model":serialized,"model_sha256":lab.digest(serialized),
            "fit_rows":rows,"fit_rows_per_market":{s:sum(r["symbol"]==s for r in rows) for s in SYMBOLS},
            "model_available_after_utc":datasets[SYMBOLS[0]][1199].closed_at.isoformat()})
    except Exception as exc:
        _append(root,"FIT_FAILED",{"error_code":"FIT_EVIDENCE_UNAVAILABLE","error_type":type(exc).__name__})
        return review(root)
    for symbol in SYMBOLS:
        try:
            bars=datasets[symbol];folds=[]
            for n,(start,end) in enumerate(RULES["evaluation_windows"],1):
                reader=RegimeReader(bars[:end],cap=end,model=model)
                folds.append({"fold":n,"window":[start,end],
                    "forecasts":forecast_diagnostics(bars[:end],reader,model,start,end),
                    "arms":{arm:measure(arm,bars,reader,start,end) for arm in ARMS}})
            value,kind={"symbol":symbol,"folds":folds},"RESULT"
        except Exception as exc:
            value,kind={"symbol":symbol,"error_code":"EVALUATION_EVIDENCE_UNAVAILABLE","error_type":type(exc).__name__},"FAILED"
        _append(root,kind,value)
        print(lab.canonical({"symbol":symbol,"status":kind}),flush=True)
    return review(root)


def review(root):
    with closing(_connect(root,readonly=True)) as db:
        db.execute("BEGIN");events,head=_events(db)
    cases=[]
    for kind,v in events:
        if kind not in ("RESULT","FAILED"):continue
        reasons=[]
        if kind=="FAILED":reasons=[v["error_code"]]
        else:
            for fold in v["folds"]:
                reasons.extend(f"FOLD_{fold['fold']}_"+r for r in gate(fold["arms"][MODEL]))
                for scenario in SCENARIOS:
                    model_net=fold["arms"][MODEL][scenario]["net_including_boundary_mark"]
                    for control in ARMS[1:]:
                        if model_net<=fold["arms"][control][scenario]["net_including_boundary_mark"]:
                            reasons.append(f"FOLD_{fold['fold']}_{scenario}_NOT_BETTER_THAN_{control}")
        cases.append({**v,"research_screen_reasons":reasons,"research_screen_passed":not reasons})
    fitted=next((v for k,v in events if k=="FIT"),None)
    return {"engine":ENGINE,"manifest":events[0][1],"journal_head":head,
        "model":fitted,"complete":len(cases)==3,"evaluation_cases":len(cases),
        "evaluation_failed":sum("error_code" in c for c in cases),"cases":cases,
        "research_screen_passed":sum(c["research_screen_passed"] for c in cases),
        "fit_failed":any(k=="FIT_FAILED" for k,v in events),"statistical_significance_established":False,
        "independent_project_out_of_sample":False,"final_reserve_evaluated":False,
        "prospective_tracking_started":False,"profitability_proven":False,"execution_authorized":False,
        "automatic_promotion_authorized":False}

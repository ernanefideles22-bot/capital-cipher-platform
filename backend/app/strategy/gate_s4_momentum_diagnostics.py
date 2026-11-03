"""Fixed descriptive diagnostics, not another strategy or exit optimization."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from urllib.parse import parse_qs, urlsplit

from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_slow_momentum_study as parent
from app.strategy.gate_s4_real_source import ORIGIN, load_real_dataset
from app.strategy.gate_s4_slow_momentum import MomentumReader, POLICY_ID

ENGINE = "S4_MOMENTUM_DESCRIPTIVE_DIAGNOSIS_V1"
PROTOCOL = {"markets": ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "timeframe": "4h",
    "horizons_bars": [6, 12, 30], "entry_lag_bars": 2,
    "cohorts": ["ALL_SIGNALS", "NONOVERLAPPING_SIGNALS", "EXECUTED_NOMINAL_TRADES"],
    "diagnostic_case_budget": 3, "summary_slots": 27, "new_strategy_trial_budget": 0,
    "parameter_optimization": False, "partial_horizon_samples": False,
    "financial_window": "[220,floor(.6*N))", "post_hoc_descriptive": True,
    "independent_validation": False, "statistical_significance_testing": False,
    "benchmark": "SAME_EVENT_TIME_ALWAYS_LONG_PRICE_MARK_UNMATCHED_EXPOSURE",
    "raw_mapping": "t+14400; o/h/l/c direct; volume=v*archived_multiplier",
    "execution_authorized": False, "automatic_promotion_authorized": False}


def fingerprint():
    return lab.digest({"parent": parent.fingerprint(),
        "diagnostic": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def verify_raw(bars, archive):
    """Independent dictionary mapping: never call normalize_candles here."""
    metadata, records, receipts = None, {}, {}
    contract = bars[0].symbol[:-4] + "_USDT"
    pages = archive["raw_pages"]
    for page in pages:
        raw = page["raw_response_utf8"].encode()
        if hashlib.sha256(raw).hexdigest() != page["response_sha256"]:
            raise ValueError("Raw response checksum mismatch")
        url = urlsplit(page["url"])
        if url.scheme != "https" or url.netloc != urlsplit(ORIGIN).netloc:
            raise ValueError("Unexpected raw source origin")
        value = json.loads(raw)
        if url.path == "/api/v4/futures/usdt/contracts/" + contract:
            if metadata is not None or value["name"] != contract:
                raise ValueError("Unexpected/duplicate contract metadata")
            metadata = Decimal(str(value["quanto_multiplier"]))
        elif url.path == "/api/v4/futures/usdt/candlesticks":
            params = parse_qs(url.query)
            if params.get("contract") != [contract] or params.get("interval") != ["4h"]:
                raise ValueError("Raw contract/timeframe mismatch")
            for row in value:
                stamp = Decimal(str(row["t"]))
                if stamp != stamp.to_integral_value() or int(stamp) % 14400:
                    raise ValueError("Misaligned raw candle timestamp")
                closed = int(stamp) + 14400
                if closed in records:
                    raise ValueError("Duplicate raw candle")
                records[closed] = row
                receipts[closed] = datetime.fromisoformat(page["received_at_utc"])
        else:
            raise ValueError("Unregistered raw endpoint")
    if metadata is None or not metadata.is_finite() or metadata <= 0 or len(records) != len(bars):
        raise ValueError("Incomplete raw provenance")
    for b in bars:
        stamp = int(b.closed_at.timestamp())
        row = records[stamp]
        for name, key in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
            if getattr(b, name) != float(Decimal(str(row[key]))):
                raise ValueError("Canonical/raw OHLC mismatch")
        if b.volume != float(Decimal(str(row["v"])) * metadata) or b.received_at != receipts[stamp]:
            raise ValueError("Canonical/raw volume or receipt mismatch")
    return {"raw_candles_verified": len(bars), "raw_pages_verified": len(pages),
        "ohlc_volume_timestamps_and_receipts_match": True,
        "historical_contract_multiplier_changes_independently_verified": False}


def event_rows(bars, reader, trades, *, start, end):
    """All outputs stay inside train; a full 30-bar horizon is mandatory."""
    if not 220 <= start < end <= len(bars):
        raise ValueError("Invalid diagnostic window")
    actual = {t["decision_bar_index"]: t for t in trades}
    if len(actual) != len(trades):
        raise ValueError("Repeated nominal entry decision")
    rows, excluded, last_nonoverlap = [], 0, -1
    for i in range(start, end):
        d = reader.decision(POLICY_ID, i + 1, bars[i].closed_at + timedelta(microseconds=1))
        if d.action == "WAIT":
            continue
        if d.action not in ("LONG", "SHORT") or d.execution_authorized or d.predictive_edge_proven:
            raise ValueError("Unsupported diagnostic signal")
        j = i + PROTOCOL["entry_lag_bars"]
        if j + max(PROTOCOL["horizons_bars"]) > end:
            excluded += 1
            continue
        if bars[j - 1].closed_at <= d.available_at:
            raise ValueError("Noncausal diagnostic entry")
        if i in actual and (actual[i]["entry_bar_index"] != j or actual[i]["side"] != d.action):
            raise ValueError("Recorded nominal trade does not match frozen signal/clock")
        direction = 1 if d.action == "LONG" else -1
        nonoverlap = j > last_nonoverlap
        if nonoverlap:
            last_nonoverlap = j + max(PROTOCOL["horizons_bars"]) - 1
        rows.append({"decision_index": i, "entry_index": j, "side": d.action,
            "nonoverlapping": nonoverlap, "executed_nominal_trade": i in actual,
            "raw_entry_open": bars[j].open,
            "marks": {str(h): {"last_index": j + h - 1,
                "signed_price_bps": direction * (bars[j + h - 1].close / bars[j].open - 1) * 10000,
                "same_time_long_price_bps": (bars[j + h - 1].close / bars[j].open - 1) * 10000}
                for h in PROTOCOL["horizons_bars"]}})
    return rows, excluded


def summaries(rows):
    result = []
    for cohort in PROTOCOL["cohorts"]:
        chosen = [r for r in rows if cohort == "ALL_SIGNALS" or
            (r["nonoverlapping"] if cohort == "NONOVERLAPPING_SIGNALS" else r["executed_nominal_trade"])]
        for h in PROTOCOL["horizons_bars"]:
            values = [r["marks"][str(h)]["signed_price_bps"] for r in chosen]
            longs = [r["marks"][str(h)]["same_time_long_price_bps"] for r in chosen]
            result.append({"cohort": cohort, "horizon_bars": h, "observations": len(chosen),
                "mean_signed_price_bps": fmean(values) if values else None,
                "median_signed_price_bps": median(values) if values else None,
                "positive_price_fraction": sum(x > 0 for x in values) / len(values) if values else None,
                "mean_same_time_long_price_bps": fmean(longs) if longs else None,
                "mean_signed_minus_same_time_long_bps": fmean(a-b for a,b in zip(values,longs)) if values else None,
                "entry_costs_stops_risk_limits_or_reentry_modeled": False,
                "independent_observations_established": False, "tradable_pnl_estimate": False})
    return result


def trade_paths(bars, trades, *, end):
    groups, records = {}, []
    for t in trades:
        i,j,k = t["decision_bar_index"],t["entry_bar_index"],t["exit_bar_index"]
        if not 220 <= i < j <= k < end or t["strategy_id"] != POLICY_ID:
            raise ValueError("Trade outside registered training window")
        direction = 1 if t["side"] == "LONG" else -1
        raw_entry = bars[j].open
        if not math.isclose(t["entry_price"], raw_entry * (1 + direction * .0006), abs_tol=1e-8):
            raise ValueError("Nominal entry does not reconcile")
        raw_exit = t["exit_price"] / (1 - direction * .0006)
        raw_gross = direction * (raw_exit - raw_entry) * t["quantity"]
        slippage = raw_gross - t["gross_pnl"]
        if not math.isclose(raw_gross-slippage-t["fees_paid"]-t["funding_paid"],t["net_pnl"],abs_tol=1e-8):
            raise ValueError("Trade cost attribution mismatch")
        distance = abs(t["entry_price"]-t["stop_price"])
        # Exclude exit candle: its high/low may have occurred after the exit.
        before = bars[j:k]
        favorable = max([0.] + [(b.high-t["entry_price"] if direction==1 else t["entry_price"]-b.low)/distance for b in before])
        adverse = max([0.] + [(t["entry_price"]-b.low if direction==1 else b.high-t["entry_price"])/distance for b in before])
        record = {"decision_index":i,"entry_index":j,"exit_index":k,"exit_reason":t["exit_reason"],
            "raw_gross_on_executed_path":raw_gross,"slippage_drag":slippage,
            "fees":t["fees_paid"],"funding":t["funding_paid"],"net":t["net_pnl"],
            "completed_bar_favorable_excursion_r_before_exit":favorable,
            "completed_bar_adverse_excursion_r_before_exit":adverse,
            "exit_bar_high_low_excluded":True,"exact_intrabar_excursion_known":False}
        records.append(record)
        group=groups.setdefault(t["exit_reason"],{"trades":0,"raw_gross":0.,"slippage":0.,"fees":0.,"funding":0.,"net":0.})
        group["trades"]+=1
        for a,b in (("raw_gross","raw_gross_on_executed_path"),("slippage","slippage_drag"),("fees","fees"),("funding","funding"),("net","net")):
            group[a]+=record[b]
    return {"records":records,"exit_groups":groups,"zero_cost_counterfactual_replay":False,
        "exit_parameters_optimized":False,"prices_after_exit_treated_as_realized_profit":False}


def _path(root):
    p=lab._db_path(root).with_name("s4-momentum-diagnostics.sqlite3")
    if p.is_symlink():raise ValueError("Journal symlink refused")
    return p


def _connect(root, readonly=False):
    db=sqlite3.connect(_path(root).as_uri()+("?mode=ro" if readonly else "?mode=rw"),uri=True)
    if readonly:db.execute("PRAGMA query_only=ON")
    return db


def _events(db):
    events,previous=[],lab.GENESIS
    for expected,(seq,kind,payload,prev,sha) in enumerate(db.execute(
        "SELECT seq,kind,payload,previous_hash,event_hash FROM events ORDER BY seq"),1):
        if expected>3 or seq!=expected or prev!=previous or sha!=lab.digest([prev,kind,payload]):
            raise ValueError("Diagnostic journal integrity/budget failure")
        events.append((kind,json.loads(payload)));previous=sha
    if not events or events[0][0]!="REGISTERED":raise ValueError("Registration missing")
    m=events[0][1]
    if m["engine"]!=ENGINE or m["engine_sha256"]!=fingerprint() or m["protocol"]!=PROTOCOL:
        raise ValueError("Frozen diagnostic implementation/protocol changed")
    if len(events)>1 and events[1][0]!="STARTED":raise ValueError("Diagnostic start missing")
    if len(events)>2 and events[2][0] not in ("RESULT","FAILED"):raise ValueError("Unexpected diagnostic phase")
    return events,previous


def register(root, parent_root):
    r=parent.review(parent_root)
    datasets=r["manifest"]["datasets"]
    if (not r["complete"] or r["training_failed"] or len(r["results"])!=3 or len(datasets)!=3
        or {(d["symbol"],d["timeframe"]) for d in datasets}!={(s,"4h") for s in PROTOCOL["markets"]}):
        raise ValueError("Completed three-market parent evidence required")
    m={"engine":ENGINE,"engine_sha256":fingerprint(),"protocol":PROTOCOL,
        "registered_at_utc":datetime.now(timezone.utc).isoformat(),
        "parent_root":str(Path(parent_root).resolve()),"parent_journal_head":r["journal_head"],
        "parent_db_sha256":hashlib.sha256(parent._path(parent_root).read_bytes()).hexdigest(),
        "parent_results_already_known":True,"datasets":r["manifest"]["datasets"],
        "catalog_path":r["manifest"]["catalog_path"],"profitability_proven":False}
    p=_path(root);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("xb"):pass
    with closing(_connect(root)) as db,db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        lab._append(db,"REGISTERED",m)
    return m


def run(root, source_root=None, parent_root=None):
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");events,_=_events(db)
        if len(events)!=1:raise ValueError("Diagnostic already started; no retry/reset")
        m=events[0][1]
        origin=Path(parent_root) if parent_root else Path(m["parent_root"])
        r=parent.review(origin)
        if (r["journal_head"]!=m["parent_journal_head"] or
            hashlib.sha256(parent._path(origin).read_bytes()).hexdigest()!=m["parent_db_sha256"]):
            raise ValueError("Parent evidence changed")
        lab._append(db,"STARTED",{"at_utc":datetime.now(timezone.utc).isoformat()})
    try:
        source=Path(source_root) if source_root else Path(m["catalog_path"]).parent
        cases=[]
        for d in m["datasets"]:
            path=source/d["relative_path"];bars,sha=load_real_dataset(path)
            raw=path.with_name("source.json").read_bytes()
            if sha!=d["sha256"] or hashlib.sha256(raw).hexdigest()!=d["source_archive_sha256"]:
                raise ValueError("Registered data/source changed")
            provenance=verify_raw(bars,json.loads(raw))
            end=int(.6*len(bars));visible=bars[:end];reader=MomentumReader(visible,cap=end)
            trade=next(v for v in r["results"] if v["symbol"]==d["symbol"])["scenarios"]["nominal"]["trades"]
            rows,excluded=event_rows(visible,reader,trade,start=220,end=end)
            paths=trade_paths(visible,trade,end=end)
            cases.append({"symbol":d["symbol"],"window":[220,end],"provenance":provenance,
                "events":rows,"incomplete_horizon_signals_excluded":excluded,
                "summaries":summaries(rows),"trade_paths":paths})
        if len(cases)!=3 or sum(len(c["summaries"]) for c in cases)!=27:
            raise ValueError("Incomplete registered diagnostic budget")
        value,kind={"cases":cases,"new_strategy_trials":0,"validation_evaluated":False,
            "final_reserve_evaluated":False,"profitability_proven":False,
            "execution_authorized":False,"automatic_promotion_authorized":False},"RESULT"
    except Exception as exc:
        value,kind={"error_type":type(exc).__name__,"error_code":"DIAGNOSTIC_EVIDENCE_UNAVAILABLE"},"FAILED"
    with closing(_connect(root)) as db,db:
        db.execute("BEGIN IMMEDIATE");_events(db);lab._append(db,kind,value)
    return review(root)


def review(root):
    with closing(_connect(root,readonly=True)) as db:
        db.execute("BEGIN");events,head=_events(db)
    return {"engine":ENGINE,"manifest":events[0][1],"journal_head":head,
        "status":events[-1][0],"complete":events[-1][0]=="RESULT",
        "result":events[-1][1] if len(events)==3 else None}

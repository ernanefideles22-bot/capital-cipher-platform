"""Preregistered archive-source adaptation; V1 economics and dates unchanged."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from app.strategy import gate_s4_funding_feasibility as v1

ENGINE = "S4_BTC_FUNDING_OFFICIAL_ARCHIVE_TRANSPORT_V2"
MONTHS = ("202404", "202405", "202406", "202407", "202408", "202409")
ARCHIVE_ORIGIN = "https://download.gatedata.org"


def fingerprint():
    return v1.digest({"v1_sha256": v1.fingerprint(),
                      "v2_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def rules():
    return {"paired_economics_v1": v1.manifest(), "transport_engine": ENGINE,
            "funding_source": "official monthly futures_usdt/funding_applies CSV.gz",
            "official_description": "https://www.gate.com/developer/historical_quotes",
            "months": list(MONTHS), "source_attempt_budget": 1, "public_request_budget": 15,
            "monthly_files_include_unscored_rows_outside_fixed_window": True,
            "date_rule_cost_or_risk_changes": False, "prior_failed_attempt_preserved": True}


def _events(db):
    values, previous = [], v1.GENESIS
    for seq, kind, payload, prev, sha in db.execute("SELECT * FROM events ORDER BY seq"):
        if seq != len(values) + 1 or prev != previous or sha != v1.digest([prev, kind, payload]):
            raise ValueError("Archive journal chain mismatch")
        values.append((kind, json.loads(payload)))
        previous = sha
    kinds = [k for k, _ in values]
    legal = [["REGISTERED"], ["REGISTERED", "STARTED"],
             ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"],
             ["REGISTERED", "STARTED", "SOURCE_AVAILABLE"],
             ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "RESULT"],
             ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "FAILED"]]
    if kinds not in legal or values[0][1]["rules"] != rules() or values[0][1]["source_sha256"] != fingerprint():
        raise ValueError("Frozen archive source/rules or phase mismatch")
    return values, previous


def register(root, prior_root):
    prior = v1.review(prior_root)
    receipt_path = Path(prior_root) / "source/funding-01.json"
    receipt = json.loads(receipt_path.read_bytes())
    if (prior["phases"] != ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"] or
            prior["result"] is not None or prior["source"]["reason"] != "SOURCE_HTTP_400_funding" or
            receipt.get("status") != 400 or receipt.get("kind") != "funding" or
            hashlib.sha256(receipt["raw_response_utf8"].encode()).hexdigest() != receipt["response_sha256"] or
            json.loads(receipt["raw_response_utf8"]).get("message") != "from time exceeds 180-day limit"):
        raise ValueError("Only preserved V1 no-PnL funding-retention failure permits V2")
    m = {"engine": ENGINE, "source_sha256": fingerprint(), "rules": rules(),
         "registered_at_utc": datetime.now(timezone.utc).isoformat(),
         "prior_journal_head": prior["journal_head"],
         "prior_db_sha256": hashlib.sha256(v1._path(prior_root).read_bytes()).hexdigest(),
         "prior_raw_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
         "no_prior_financial_results": True}
    p = v1._path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("xb"):
        pass
    with closing(v1._connect(root)) as db, db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,
            payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        v1._append(db, "REGISTERED", m)
    return m


def archive_get(month):
    if month not in MONTHS:
        raise ValueError("Only preregistered official funding archives")
    url = f"{ARCHIVE_ORIGIN}/futures_usdt/funding_applies/{month}/BTC_USDT-{month}.csv.gz"
    receipt = {"url": url, "month": month, "requested_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        with build_opener(v1._NoRedirect()).open(Request(url, method="GET"), timeout=30) as response:
            if response.status != 200 or response.url != url:
                raise ValueError("Unexpected archive response/redirect")
            raw, status = response.read(2 * 1024 * 1024 + 1), 200
    except HTTPError as error:
        raw, status = error.read(2 * 1024 * 1024 + 1), error.code
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Compressed funding archive byte budget")
    receipt.update(status=status, received_at_utc=datetime.now(timezone.utc).isoformat(),
                   response_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    return receipt, raw


def parse_archive(raw, month):
    if month not in MONTHS:
        raise ValueError("Unregistered funding month")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
        data = f.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Decompressed funding archive byte budget")
    result = []
    for row in csv.reader(io.StringIO(data.decode("utf-8-sig"))):
        if not row or all(not c.strip() for c in row):
            continue
        if [c.strip().lower() for c in row] == ["timestamp", "funding_rate"]:
            if result:
                raise ValueError("Unexpected repeated archive header")
            continue
        if len(row) != 2:
            raise ValueError("Official funding-applies two-column schema required")
        t, r = map(v1._number, row)
        if (not t.is_integer() or abs(r) > .1 or
                datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m") != month):
            raise ValueError("Funding archive month/rate mismatch")
        if v1.START <= t <= v1.END:
            result.append({"t": int(t), "r": r})
    return result


def capture(root):
    folder = Path(root) / "source"
    folder.mkdir(exist_ok=False)
    series = {"funding": []}
    for month in MONTHS:
        receipt, raw = archive_get(month)
        binary = folder / f"funding-{month}.csv.gz"
        with binary.open("xb") as f:
            f.write(raw)
        with (folder / f"funding-{month}.json").open("x") as f:
            json.dump(receipt, f, indent=2)
        if receipt["status"] != 200:
            raise ValueError(f"ARCHIVE_HTTP_{receipt['status']}_{month}")
        series["funding"].extend(parse_archive(raw, month))
    series["funding"].sort(key=lambda r: r["t"])
    v1.normalize("funding", series["funding"], v1.START, v1.END)
    for kind in ("spot", "perp", "mark"):
        values, lower, page = [], v1.START, 0
        while lower <= v1.END:
            upper = min(v1.END, lower + 500 * v1.STEP - 1)
            receipt = v1.public_get(kind, lower, upper)
            page += 1
            with (folder / f"{kind}-{page:02}.json").open("x") as f:
                json.dump(receipt, f, indent=2, allow_nan=False)
            if receipt["status"] != 200:
                raise ValueError(f"SOURCE_HTTP_{receipt['status']}_{kind}")
            values.extend(v1.normalize(kind, json.loads(receipt["raw_response_utf8"]), lower, upper))
            lower = upper + 1
        series[kind] = values
    return series


def _record(root, kind, value):
    with closing(v1._connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _events(db)
        v1._append(db, kind, value)


def run(root):
    with closing(v1._connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if len(events) != 1:
            raise ValueError("Archive attempt already started; no retry/reset")
        v1._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    try:
        series = capture(root)
        v1.validate_series(series)
        with (Path(root) / "normalized-series.json").open("x") as f:
            f.write(v1.canonical(series))
        _record(root, "SOURCE_AVAILABLE", {"data_sha256": v1.digest(series),
                "rows": {k: len(v) for k, v in series.items()}, "raw_receipts": 15})
    except Exception as exc:
        _record(root, "SOURCE_UNAVAILABLE", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
        return review(root)
    try:
        result = v1.evaluate(series)
    except Exception as exc:
        _record(root, "FAILED", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
    else:
        _record(root, "RESULT", result)
    return review(root)


def review(root):
    with closing(v1._connect(root, readonly=True)) as db:
        values, head = _events(db)
    return {"engine": ENGINE, "journal_head": head, "phases": [k for k, _ in values],
            "registration": values[0][1], "source": next((v for k, v in values if k.startswith("SOURCE_")), None),
            "result": next((v for k, v in values if k == "RESULT"), None),
            "profitability_proven": False, "execution_authorized": False, "eligible_for_s5": False}

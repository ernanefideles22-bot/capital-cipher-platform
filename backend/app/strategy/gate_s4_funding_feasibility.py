"""Isolated historical economics of equal-quantity spot long / perpetual short.

Not a trading agent: no order routing, stops, liquidation engine or promotion.
Actual funding rates replace constant funding assumptions. Coarse price marks
bound payment uncertainty; six fixed historical blocks are exploratory only.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENGINE = "S4_BTC_SPOT_PERP_FUNDING_ECONOMICS_V1"
ORIGIN = "https://api.gateio.ws/api/v4"
STEP = 14400
DAY = 86400
START = int(datetime(2024, 4, 1, tzinfo=timezone.utc).timestamp())
END = START + 180 * DAY
SCENARIOS = {
    "nominal": {"spot_fee_bps": 20, "perp_fee_bps": 6, "slip_bps": 6, "delay_bars": 0},
    "stress": {"spot_fee_bps": 20, "perp_fee_bps": 12, "slip_bps": 12, "delay_bars": 0},
    "stress_delayed": {"spot_fee_bps": 20, "perp_fee_bps": 12, "slip_bps": 12, "delay_bars": 1},
}
GENESIS = "0" * 64


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def manifest():
    return {
        "engine": ENGINE, "symbol": "BTC_USDT", "interval_seconds": STEP,
        "start": START, "end": END, "blocks": 6, "block_days": 30,
        "economic_hypotheses": 1, "scenario_references": 18, "parameter_search_budget": 0,
        "capital_usdt_per_block": 1000, "spot_budget_usdt": 400,
        "short_margin_usdt": 400, "cash_reserve_usdt": 200,
        "quantity": "400 / adversely slipped spot entry; equal base units on short",
        "entry": "first 4h open strictly after calendar boundary, plus registered delay",
        "exit": "next block boundary open, regardless of funding or returns",
        "funding": "settled r; entry < t < exit; q * mark candle open * r",
        "funding_bounds": "conditional q*r*[low,high] across both candles adjacent to t; swap bounds for negative r",
        "funding_grid_seconds": 28800, "assumed_funding_schedule_certified_historical": False,
        "funding_at_boundary": "excluded at entry and exit due to settlement ordering ambiguity",
        "scenarios": SCENARIOS, "funding_not_replaced_with_fixed_rate": True,
        "screen": {"all_blocks_all_scenarios_positive_net_lower_bound": True,
                   "minimum_funding_events_per_block": 88,
                   "max_close_drawdown_pct": 5, "minimum_margin_buffer_fraction": .2},
        "margin_proxy": "400 + short adverse-close mark PnL + cumulative adverse funding - perp entry/exit fees; minus 2% mark high notional",
        "maintenance_margin_assumption": .02,
        "margin_proxy_not_liquidation_simulation": True,
        "risk_stops_simulated": False, "risk_engine_modified": False,
        "funding_mark_is_4h_proxy_not_settlement_tick": True,
        "historical_training_overlap": True, "prospective_evidence": False,
        "existing_reserve_evaluated": False, "globally_unseen_data_certified": False,
        "parent_pr": 95, "parent_commit": "ab95dfc3ace96716f3b10337bded6818d6e01686",
        "prior_decision_report": "libfile_ff9499473dbc8191bd246cc6e49c569d",
        "training_scope_report": "libfile_44ca77f977348191b6cdcf5aa272b9d8",
        "window_reason": "first full calendar quarter start inside already-used 4h training, followed by fixed 180 days",
        "not_an_executable_strategy": True, "no_borrowing_or_interest_income": True,
        "account_fees_and_historical_lot_rules_verified": False,
        "fees_are_explicit_assumptions_not_account_quotes": True,
        "leg_atomicity_liquidity_and_custody_risks_modelled": False,
        "profitability_proven": False, "execution_authorized": False, "automatic_promotion_authorized": False,
        "public_request_budget": 16, "source_attempt_budget": 1,
    }


def fingerprint():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _path(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Journal symlink refused")
    target = root / "funding-economics.sqlite3"
    if target.is_symlink():
        raise ValueError("Journal symlink refused")
    return target


def _connect(root, readonly=False):
    db = sqlite3.connect(_path(root).as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


def _append(db, kind, value):
    previous = db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    previous = previous[0] if previous else GENESIS
    payload = canonical(value)
    db.execute("INSERT INTO events(kind,payload,previous_hash,event_hash) VALUES(?,?,?,?)",
               (kind, payload, previous, digest([previous, kind, payload])))


def _events(db):
    result, previous = [], GENESIS
    for seq, kind, payload, prev, sha in db.execute("SELECT * FROM events ORDER BY seq"):
        if seq != len(result) + 1 or prev != previous or sha != digest([prev, kind, payload]):
            raise ValueError("Journal chain mismatch")
        result.append((kind, json.loads(payload)))
        previous = sha
    kinds = [k for k, _ in result]
    legal = [
        ["REGISTERED"], ["REGISTERED", "STARTED"],
        ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"],
        ["REGISTERED", "STARTED", "SOURCE_AVAILABLE"],
        ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "RESULT"],
        ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "FAILED"],
    ]
    if kinds not in legal or result[0][1]["rules"] != manifest() or result[0][1]["source_sha256"] != fingerprint():
        raise ValueError("Frozen rules/source or phase mismatch")
    return result, previous


def register(root):
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("xb"):
        pass
    m = {"registered_at_utc": datetime.now(timezone.utc).isoformat(),
         "source_sha256": fingerprint(), "rules": manifest()}
    with closing(_connect(root)) as db, db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,
            payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        _append(db, "REGISTERED", m)
    return m


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_get(kind, lower, upper):
    if kind not in ("spot", "perp", "mark", "funding") or not START <= lower <= upper <= END + STEP - 1:
        raise ValueError("Only fixed-range whitelisted public source GETs")
    if kind == "spot":
        path = "/spot/candlesticks"
        params = {"currency_pair": "BTC_USDT", "interval": "4h", "from": lower, "to": upper}
    elif kind == "funding":
        path = "/futures/usdt/funding_rate"
        params = {"contract": "BTC_USDT", "from": lower, "to": upper, "limit": 1000}
    else:
        path = "/futures/usdt/candlesticks"
        params = {"contract": ("mark_" if kind == "mark" else "") + "BTC_USDT",
                  "interval": "4h", "from": lower, "to": upper}
    url = ORIGIN + path + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    receipt = {"kind": kind, "url": url, "requested_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        with build_opener(_NoRedirect()).open(request, timeout=25) as response:
            if response.status != 200 or response.url != url:
                raise ValueError("Unexpected public source or redirect")
            raw = response.read(2 * 1024 * 1024 + 1)
        status = 200
    except HTTPError as error:
        raw, status = error.read(2 * 1024 * 1024 + 1), error.code
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Response byte budget exceeded")
    receipt.update(status=status, received_at_utc=datetime.now(timezone.utc).isoformat(),
                   raw_response_utf8=raw.decode(), response_sha256=hashlib.sha256(raw).hexdigest())
    return receipt


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean price/rate refused")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError("Nonfinite price/rate")
    return v


def normalize(kind, rows, lower, upper):
    if not isinstance(rows, list):
        raise ValueError("Public list required")
    result = []
    for row in rows:
        if kind == "funding":
            t, r = _number(row["t"]), _number(row["r"])
            if not t.is_integer() or abs(r) > .1:
                raise ValueError("Funding timestamp/rate bounds")
            value = {"t": int(t), "r": r}
        else:
            if kind == "spot":
                if (len(row) < 6 or
                        (len(row) >= 7 and str(row[-1]).lower() in ("false", "true") and
                         str(row[-1]).lower() != "true")):
                    raise ValueError("Incomplete spot candle")
                t = _number(row[0])
                prices = dict(zip(("c", "h", "l", "o"), map(_number, row[2:6])))
            else:
                t = _number(row["t"])
                prices = {k: _number(row[k]) for k in ("o", "h", "l", "c")}
            if (not t.is_integer() or min(prices.values()) <= 0 or
                    prices["l"] > min(prices["o"], prices["c"]) or
                    prices["h"] < max(prices["o"], prices["c"])):
                raise ValueError("Invalid OHLC/timestamp")
            value = {"t": int(t), **prices}
        if not lower <= value["t"] <= upper:
            raise ValueError("Response outside registered request range")
        result.append(value)
    result.sort(key=lambda v: v["t"])
    interval = 28800 if kind == "funding" else STEP
    expected = list(range(((lower + interval - 1) // interval) * interval, upper + 1, interval))
    if [v["t"] for v in result] != expected:
        raise ValueError("Missing/duplicate/irregular source grid; no imputation")
    return result


def capture(root):
    """Persist each raw receipt before interpreting it; never retries failed pages."""
    folder = Path(root) / "source"
    folder.mkdir(exist_ok=False)
    series = {}
    for kind in ("funding", "spot", "perp", "mark"):
        values, page = [], 0
        # Three <=500-candle pages for each price; six calendar funding pages.
        width = 30 * DAY if kind == "funding" else 500 * STEP
        lower = START
        while lower <= END:
            upper = min(END, lower + width - 1)
            receipt = public_get(kind, lower, upper)
            page += 1
            with (folder / f"{kind}-{page:02}.json").open("x") as f:
                json.dump(receipt, f, indent=2, allow_nan=False)
            if receipt["status"] != 200:
                raise ValueError(f"SOURCE_HTTP_{receipt['status']}_{kind}")
            values.extend(normalize(kind, json.loads(receipt["raw_response_utf8"]), lower, upper))
            lower = upper + 1
        # Funding has a seventh, one-point page to verify end-boundary coverage.
        series[kind] = values
    return series


def validate_series(series):
    if set(series) != {"spot", "perp", "mark", "funding"}:
        raise ValueError("Four aligned sources required")
    for kind, rows in series.items():
        expected = list(range(START, END + 1, 28800 if kind == "funding" else STEP))
        if [r["t"] for r in rows] != expected:
            raise ValueError("Full source alignment/range mismatch")
        # Validate the normalized form again, including finite prices and funding.
        raw = ([[v["t"], 0, v["c"], v["h"], v["l"], v["o"]] for v in rows]
               if kind == "spot" else rows)
        normalize(kind, raw, START, END)


def _payment(q, rate, mark, previous=None):
    point = q * rate * mark["o"]
    previous = previous or mark
    bounds = sorted((q * rate * min(mark["l"], previous["l"]),
                     q * rate * max(mark["h"], previous["h"])))
    return point, bounds[0], bounds[1]


def evaluate_block(series, block, scenario):
    """One constant-quantity economics reference; no risk-managed execution claim."""
    if type(block) is not int or not 0 <= block < 6 or scenario not in SCENARIOS:
        raise ValueError("Fixed block/scenario budget")
    cfg = SCENARIOS[scenario]
    lower, exit_t = START + block * 30 * DAY, START + (block + 1) * 30 * DAY
    entry_t = lower + STEP * (1 + cfg["delay_bars"])
    spot, perp, mark = ({r["t"]: r for r in series[k]} for k in ("spot", "perp", "mark"))
    slip = cfg["slip_bps"] / 10000
    spot_in = spot[entry_t]["o"] * (1 + slip)
    short_in = perp[entry_t]["o"] * (1 - slip)
    q = 400 / spot_in
    if q * (spot_in + short_in) > 1000:
        raise ValueError("Initial gross exposure exceeds frozen capital")
    entry_fee = q * (spot_in * cfg["spot_fee_bps"] + short_in * cfg["perp_fee_bps"]) / 10000
    perp_entry_fee = q * short_in * cfg["perp_fee_bps"] / 10000
    payments = {r["t"]: r["r"] for r in series["funding"] if entry_t < r["t"] < exit_t}
    paid, paid_low, paid_high = 0., 0., 0.
    peak, dd, min_buffer = 1000., 0., math.inf
    trajectory = []
    for t in range(entry_t, exit_t, STEP):
        rate = payments.get(t, 0.)
        point, low, high = _payment(q, rate, mark[t], mark[t - STEP])
        # Opening mark is a proxy; complete candle bounds are sensitivity only.
        # For margin before any intrabar credit, ignore positive payment this bar.
        adverse_funding = paid_low + min(0., low)
        worst_mark = mark[t]["h"]
        adverse_close = worst_mark * (1 + slip)
        perp_close_fee = q * adverse_close * cfg["perp_fee_bps"] / 10000
        buffer = (400 + q * (short_in - adverse_close) + adverse_funding -
                  perp_entry_fee - perp_close_fee - .02 * q * worst_mark)
        min_buffer = min(min_buffer, buffer)
        paid, paid_low, paid_high = paid + point, paid_low + low, paid_high + high
        spot_out_mark = spot[t]["c"] * (1 - slip)
        short_out_mark = mark[t]["c"] * (1 + slip)
        exit_fee_mark = q * (spot_out_mark * cfg["spot_fee_bps"] + short_out_mark * cfg["perp_fee_bps"]) / 10000
        eq_low = (1000 + q * ((spot_out_mark - spot_in) + (short_in - short_out_mark)) +
                  paid_low - entry_fee - exit_fee_mark)
        peak = max(peak, eq_low)
        dd = max(dd, (1 - eq_low / peak) * 100)
        trajectory.append({"closed_at": t + STEP, "liquidation_value_lower_proxy": eq_low})
    spot_out = spot[exit_t]["o"] * (1 - slip)
    short_out = perp[exit_t]["o"] * (1 + slip)
    exit_fee = q * (spot_out * cfg["spot_fee_bps"] + short_out * cfg["perp_fee_bps"]) / 10000
    price_pnl = q * ((spot_out - spot_in) + (short_in - short_out))
    unslipped = q * ((spot[exit_t]["o"] - spot[entry_t]["o"]) +
                    (perp[entry_t]["o"] - perp[exit_t]["o"]))
    fees = entry_fee + exit_fee
    net = price_pnl + paid - fees
    low_net, high_net = price_pnl + paid_low - fees, price_pnl + paid_high - fees
    dd = max(dd, (1 - (1000 + low_net) / peak) * 100)
    trajectory.append({"closed_at": exit_t, "realized_net_lower_proxy": low_net})
    return {"block": block + 1, "scenario": scenario, "entry_t": entry_t, "exit_t": exit_t,
            "quantity": q, "funding_events": len(payments), "funding_positive": sum(r > 0 for r in payments.values()),
            "funding_negative": sum(r < 0 for r in payments.values()), "funding_rate_sum_bps": sum(payments.values()) * 10000,
            "spot_entry": spot_in, "perp_entry": short_in, "spot_exit": spot_out, "perp_exit": short_out,
            "spot_initial_notional": q * spot_in, "perp_initial_notional": q * short_in,
            "basis_price_pnl_before_slippage": unslipped, "slippage_cost": unslipped - price_pnl,
            "fees": fees, "funding_point_proxy": paid, "funding_lower_bound": paid_low,
            "funding_upper_bound": paid_high, "net_point_proxy": net,
            "net_lower_bound_proxy": low_net, "net_upper_bound_proxy": high_net,
            "net_without_funding_same_path": price_pnl - fees,
            "close_drawdown_lower_proxy_pct": dd,
            "minimum_short_margin_buffer_proxy": min_buffer, "margin_buffer_fraction": min_buffer / 400,
            "equity_initial": 1000, "equity_final_point_proxy": 1000 + net,
            "quantity_matched": True, "short_leg_liquidation_certified_absent": False,
            "gross_notional_dynamic_not_capped_after_entry": True,
            "trajectory": trajectory}


def evaluate(series):
    validate_series(series)
    rows = [evaluate_block(series, block, scenario) for block in range(6) for scenario in SCENARIOS]
    reasons = []
    for r in rows:
        name = f"BLOCK_{r['block']}_{r['scenario']}"
        if r["net_lower_bound_proxy"] <= 0:
            reasons.append(name + "_NONPOSITIVE_NET")
        if r["funding_events"] < 88:
            reasons.append(name + "_INSUFFICIENT_FUNDING_EVENTS")
        if r["close_drawdown_lower_proxy_pct"] > 5:
            reasons.append(name + "_DRAWDOWN")
        if r["margin_buffer_fraction"] < .2:
            reasons.append(name + "_MARGIN_PROXY")
    return {"rows": rows, "economic_screen_passed": not reasons, "reasons": reasons,
            "status": "ECONOMICS_ONLY_EXECUTION_UNVALIDATED" if not reasons else "ECONOMIC_SCREEN_REJECTED",
            "distinct_calendar_blocks": 6, "independent_observations_certified": False,
            "cash_reference_net": 0, "statistical_tests_executed": 0,
            "funding_removal_is_same_path_decomposition_not_alternative_strategy": True,
            "profitability_proven": False, "eligible_for_s5": False,
            "risk_managed_execution_verified": False, "execution_authorized": False}


def _record(root, kind, value):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _events(db)
        _append(db, kind, value)


def run(root):
    with closing(_connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if len(events) != 1:
            raise ValueError("Already started: no retry/reset or selective replacement")
        _append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    try:
        series = capture(root)
        validate_series(series)
        path = Path(root) / "normalized-series.json"
        with path.open("x") as f:
            f.write(canonical(series))
        _record(root, "SOURCE_AVAILABLE", {"data_sha256": digest(series),
                "rows": {k: len(v) for k, v in series.items()}, "raw_receipts": 16})
    except Exception as exc:
        _record(root, "SOURCE_UNAVAILABLE", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
        return review(root)
    try:
        value = evaluate(series)
    except Exception as exc:
        _record(root, "FAILED", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
    else:
        _record(root, "RESULT", value)
    return review(root)


def review(root):
    with closing(_connect(root, readonly=True)) as db:
        events, head = _events(db)
    return {"engine": ENGINE, "journal_head": head, "phases": [k for k, _ in events],
            "registration": events[0][1], "source": next((v for k, v in events if k.startswith("SOURCE_")), None),
            "result": next((v for k, v in events if k == "RESULT"), None),
            "profitability_proven": False, "execution_authorized": False, "eligible_for_s5": False}

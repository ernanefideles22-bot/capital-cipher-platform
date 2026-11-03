"""Bounded public book-cost audit; no account, orders or strategy promotion.

VWAPs are static displayed-liquidity quotes, not measured execution slippage.
Personal production fees remain unverified. Never reuse Testnet credentials.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from app.strategy import gate_s4_funding_feasibility as base

ENGINE = "GATE_PUBLIC_EXECUTION_COST_AUDIT_V1"
SYMBOLS = ("BTC_USDT", "ETH_USDT", "SOL_USDT")
TARGETS = (50, 100, 400, 1000)
SAMPLES = 5
ORIGIN = "https://api.gateio.ws/api/v4"


def rules():
    return {"engine": ENGINE, "symbols": list(SYMBOLS), "target_leg_notional_usdt": list(TARGETS),
            "samples_each": SAMPLES, "minimum_gap_seconds": 5, "book_depth_levels": 100,
            "public_get_budget": 36, "metadata_gets": 6, "book_gets": 30,
            "retry_budget": 0, "max_response_generation_age_seconds": 10,
            "max_book_update_age_seconds": 60, "max_pair_generation_skew_seconds": 5,
            "future_clock_tolerance_seconds": 2, "max_book_request_duration_seconds": 20,
            "book_time_units": {"spot": "milliseconds", "perp": "seconds"},
            "quantity_policy": "whole futures contracts; spot decimal precision; paired common lot LCM",
            "book_cost": "same-quantity displayed buy/sell VWAP minus/plus mid; includes spread once",
            "nominal_fee_assumption_bps_side": {"spot": 20, "perp": 6},
            "stress_fee_assumption_bps_side": {"spot": 20, "perp": 12},
            "personal_fee_profile_status": "UNVERIFIED_NOT_QUERIED",
            "public_contract_fee_is_personal_fee": False,
            "spot_deprecated_fee_field_used_for_cost": False,
            "metadata_scope": "current metadata; not historical lot reconstruction",
            "latency_adverse_selection_or_fill_probability_measured": False,
            "same_book_roundtrip_is_counterfactual_not_executed": True,
            "sufficient_to_lower_existing_slippage_assumptions": False,
            "strategy_replays_executed": 0, "execution_authorized": False,
            "profitability_proven": False, "oms_or_risk_modified": False,
            "previous_commit": "28650a56101b71eb3529a312e7956774b55176bd"}


def fingerprint():
    return base.digest({"audit": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        "journal_helpers": base.fingerprint()})


def _events(db):
    out, previous = [], base.GENESIS
    for seq, kind, payload, prev, sha in db.execute("SELECT * FROM events ORDER BY seq"):
        if seq != len(out) + 1 or prev != previous or sha != base.digest([prev, kind, payload]):
            raise ValueError("Cost audit journal chain mismatch")
        out.append((kind, json.loads(payload)))
        previous = sha
    kinds = [k for k, _ in out]
    prefixes = [["REGISTERED"], ["REGISTERED", "STARTED"],
                ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"]]
    if kinds not in prefixes:
        if (kinds[:3] != ["REGISTERED", "STARTED", "METADATA"] or
                kinds[3:] != ["SAMPLE"] * (len(kinds) - 3) or not 0 <= len(kinds) - 3 <= SAMPLES):
            raise ValueError("Cost audit phase/budget mismatch")
    if out[0][1]["rules"] != rules() or out[0][1]["source_sha256"] != fingerprint():
        raise ValueError("Frozen cost audit source/rules changed")
    for expected, (_, row) in enumerate(out[3:], 1):
        if row["sample"] != expected:
            raise ValueError("Repeated/reordered cost sample")
    return out, previous


def register(root):
    path = base._path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    m = {"registered_at_utc": datetime.now(timezone.utc).isoformat(),
         "rules": rules(), "source_sha256": fingerprint()}
    with closing(base._connect(root)) as db, db:
        db.executescript("""CREATE TABLE events(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL,
            payload TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL);
            CREATE TRIGGER no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;""")
        base._append(db, "REGISTERED", m)
    return m


def public_get(kind, symbol):
    if kind not in ("spot_meta", "perp_meta", "spot_book", "perp_book") or symbol not in SYMBOLS:
        raise ValueError("Only fixed public metadata and order-book GET endpoints")
    path, params = {
        "spot_meta": ("/spot/currency_pairs/" + symbol, {}),
        "perp_meta": ("/futures/usdt/contracts/" + symbol, {}),
        "spot_book": ("/spot/order_book", {"currency_pair": symbol, "limit": 100, "interval": "0", "with_id": "true"}),
        "perp_book": ("/futures/usdt/order_book", {"contract": symbol, "limit": 100, "interval": "0", "with_id": "true"}),
    }[kind]
    url = ORIGIN + path + ("?" + urlencode(params) if params else "")
    receipt = {"kind": kind, "symbol": symbol, "url": url,
               "requested_at_utc": datetime.now(timezone.utc).isoformat()}
    start = time.monotonic()
    try:
        with build_opener(base._NoRedirect()).open(Request(url, method="GET", headers={"Accept": "application/json"}), timeout=20) as response:
            if response.url != url or response.status != 200:
                raise ValueError("Unexpected public source or redirect")
            raw, status = response.read(2 * 1024 * 1024 + 1), 200
    except HTTPError as error:
        raw, status = error.read(2 * 1024 * 1024 + 1), error.code
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Public response byte budget")
    receipt.update(status=status, received_at_utc=datetime.now(timezone.utc).isoformat(),
                   elapsed_seconds=time.monotonic() - start,
                   raw_response_utf8=raw.decode(), response_sha256=hashlib.sha256(raw).hexdigest())
    return receipt


def D(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("Explicit finite decimal required")
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError("Invalid decimal") from None
    if not result.is_finite():
        raise ValueError("Nonfinite decimal")
    return result


def metadata(spot, perp, symbol):
    if (spot.get("id") != symbol or spot.get("trade_status") != "tradable" or
            perp.get("name") != symbol or perp.get("type") != "direct" or
            perp.get("in_delisting") is not False):
        raise ValueError("Unsupported/delisting/nontradable market")
    precision = spot["amount_precision"]
    if type(precision) is not int or not 0 <= precision <= 18:
        raise ValueError("Invalid spot quantity precision")
    mult = D(perp["quanto_multiplier"])
    if mult <= 0 or not 0 < D(perp["order_size_min"]) <= D(perp["order_size_max"]):
        raise ValueError("Invalid futures multiplier/size bounds")
    for key in ("min_base_amount", "min_quote_amount", "max_base_amount", "max_quote_amount"):
        if key not in spot or (spot[key] is not None and D(spot[key]) < 0):
            raise ValueError("Spot bounds must be explicit (null means no limit)")
    return {"spot": spot, "perp": perp, "multiplier": str(mult),
            "spot_quantum": str(Decimal(1).scaleb(-precision)),
            "public_perp_taker_fee_reference": perp.get("taker_fee_rate"),
            "personal_fee_verified": False}


def book(receipt, venue, multiplier=Decimal(1)):
    if venue not in ("spot", "perp") or multiplier <= 0:
        raise ValueError("Explicit venue and positive multiplier required")
    if receipt["status"] != 200 or receipt["elapsed_seconds"] > 20:
        raise ValueError("Book source unavailable or slow")
    raw = json.loads(receipt["raw_response_utf8"])
    scale = 1000 if venue == "spot" else 1
    current, update = float(D(raw["current"]) / scale), float(D(raw["update"]) / scale)
    received = datetime.fromisoformat(receipt["received_at_utc"]).timestamp()
    if (type(raw["id"]) is not int or raw["id"] < 0 or
            not -2 <= received - current <= 10 or not -2 <= received - update <= 60 or update > current + .001):
        raise ValueError("Stale/future book clock or invalid book ID")
    sides = {}
    for side in ("asks", "bids"):
        vals = []
        for v in raw[side]:
            price, qty = (D(v[0]), D(v[1])) if venue == "spot" else (D(v["p"]), D(v["s"]) * multiplier)
            if price <= 0 or qty <= 0:
                raise ValueError("Nonpositive book price/quantity")
            vals.append((price, qty))
        prices = [p for p, _ in vals]
        if not prices or len(prices) != len(set(prices)) or prices != sorted(prices, reverse=side == "bids"):
            raise ValueError("Empty/duplicate/unsorted book")
        sides[side] = vals
    if sides["bids"][0][0] >= sides["asks"][0][0]:
        raise ValueError("Crossed/locked book refused")
    return {**sides, "mid": (sides["asks"][0][0] + sides["bids"][0][0]) / 2,
            "current": current, "update": update, "id": raw["id"]}


def common_lot(spot_quantum, multiplier):
    if min(spot_quantum, multiplier) <= 0:
        raise ValueError("Positive lot increments required")
    power = max(0, -spot_quantum.as_tuple().exponent, -multiplier.as_tuple().exponent)
    if power > 18:
        raise ValueError("Lot precision budget exceeded")
    scale = Decimal(10) ** power
    return Decimal(math.lcm(int(spot_quantum * scale), int(multiplier * scale))) / scale


def vwap(levels, quantity):
    if quantity <= 0:
        raise ValueError("Positive quote quantity required")
    left, quote = quantity, Decimal(0)
    for price, available in levels:
        fill = min(left, available)
        quote += fill * price
        left -= fill
        if left == 0:
            return quote / quantity
    raise ValueError("INSUFFICIENT_DISPLAYED_DEPTH")


def price_quote(value, quantity):
    buy, sell = vwap(value["asks"], quantity), vwap(value["bids"], quantity)
    mid = value["mid"]
    return {"quantity": str(quantity), "mid": str(mid), "buy_vwap": str(buy), "sell_vwap": str(sell),
            "buy_quote": str(quantity * buy), "sell_quote": str(quantity * sell),
            "spread_bps": float((value["asks"][0][0] - value["bids"][0][0]) / mid * 10000),
            "buy_cost_from_mid_bps": float((buy / mid - 1) * 10000),
            "sell_cost_from_mid_bps": float((1 - sell / mid) * 10000),
            "roundtrip_displayed_price_cost_bps": float((buy - sell) / mid * 10000),
            "displayed_price_cost_usdt": str(quantity * (buy - sell)),
            "executed_fill": False}


def check_limits(meta, venue, quantity, quote):
    if venue == "spot":
        raw = meta["spot"]
        for key, val, lower in (("min_base_amount", quantity, True), ("max_base_amount", quantity, False),
                ("min_quote_amount", D(quote["sell_quote"]), True), ("max_quote_amount", D(quote["buy_quote"]), False)):
            if raw[key] is not None and (val < D(raw[key]) if lower else val > D(raw[key])):
                raise ValueError("SPOT_SIZE_BOUND_" + key)
        for key, value in (("market_order_max_stock", quantity), ("market_order_max_money", D(quote["buy_quote"]))):
            if raw.get(key) is not None and D(raw[key]) > 0 and value > D(raw[key]):
                raise ValueError("SPOT_MARKET_SIZE_CAP_" + key)
    else:
        contracts = quantity / D(meta["multiplier"])
        if contracts != contracts.to_integral_value() or not D(meta["perp"]["order_size_min"]) <= contracts <= D(meta["perp"]["order_size_max"]):
            raise ValueError("FUTURES_WHOLE_CONTRACT_SIZE_BOUND")
        market_cap = meta["perp"].get("market_order_size_max")
        if market_cap is not None and D(market_cap) > 0 and contracts > D(market_cap):
            raise ValueError("FUTURES_MARKET_SIZE_CAP")


def quote_target(meta, books, target, mode):
    try:
        venues = ("spot", "perp") if mode == "paired" else (mode,)
        step = (common_lot(D(meta["spot_quantum"]), D(meta["multiplier"])) if mode == "paired"
                else D(meta["spot_quantum"] if mode == "spot" else meta["multiplier"]))
        price = max(books[v]["asks"][0][0] for v in venues)
        quantity = (D(target) / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if quantity <= 0:
            raise ValueError("BUDGET_BELOW_AUDITED_LOT")
        quotes = {v: price_quote(books[v], quantity) for v in venues}
        for v in venues:
            check_limits(meta, v, quantity, quotes[v])
            if D(quotes[v]["buy_quote"]) > D(target):
                raise ValueError("BUY_QUOTE_EXCEEDS_TARGET_BUDGET")
        cost = sum((D(q["displayed_price_cost_usdt"]) for q in quotes.values()), Decimal(0))
        fees = {}
        for name, rates in (("nominal", {"spot": 20, "perp": 6}), ("stress", {"spot": 20, "perp": 12})):
            fee = sum(((D(q["buy_quote"]) + D(q["sell_quote"])) * rates[v] / 10000 for v, q in quotes.items()), Decimal(0))
            fees[name] = {"assumed_fees_usdt": str(fee), "displayed_price_plus_assumed_fee_usdt": str(cost + fee)}
        return {"target_leg_usdt": target, "mode": mode, "status": "STATIC_QUOTE_AVAILABLE",
                "quantity": str(quantity), "quotes": quotes, "fee_scenarios_not_personal_rates": fees,
                "target_is_per_leg_not_total_account_capital": True,
                "personal_total_cost_verified": False, "execution_authorized": False}
    except (ValueError, KeyError) as error:
        return {"target_leg_usdt": target, "mode": mode, "status": "UNAVAILABLE",
                "reason": str(error)[:160], "execution_authorized": False}


def analyze_pair(meta, spot_receipt, perp_receipt):
    books, errors = {}, {}
    for v, receipt in (("spot", spot_receipt), ("perp", perp_receipt)):
        try:
            books[v] = book(receipt, v, D(meta["multiplier"]) if v == "perp" else Decimal(1))
        except (ValueError, KeyError, TypeError) as error:
            errors[v] = str(error)[:160]
    paired_error = errors or (abs(books["spot"]["current"] - books["perp"]["current"]) > 5)
    rows = []
    for target in TARGETS:
        for mode in ("spot", "perp", "paired"):
            if (mode == "paired" and paired_error) or (mode != "paired" and mode in errors):
                rows.append({"target_leg_usdt": target, "mode": mode, "status": "UNAVAILABLE",
                             "reason": "INVALID_OR_UNSYNCHRONIZED_BOOK", "execution_authorized": False})
            else:
                rows.append(quote_target(meta, books, target, mode))
    return {"book_errors": errors, "pair_generation_skew_seconds":
            abs(books["spot"]["current"] - books["perp"]["current"]) if len(books) == 2 else None,
            "quotes": rows}


def _capture(root, kind, symbol, label):
    try:
        receipt = public_get(kind, symbol)
    except Exception as error:
        receipt = {"kind": kind, "symbol": symbol, "status": None,
                   "error_type": type(error).__name__, "received_at_utc": datetime.now(timezone.utc).isoformat()}
    folder = Path(root) / "raw"
    folder.mkdir(exist_ok=True)
    with (folder / (label + ".json")).open("x") as f:
        json.dump(receipt, f, indent=2, allow_nan=False)
    return receipt


def _record(root, kind, value):
    with closing(base._connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _events(db)
        base._append(db, kind, value)


def run(root):
    with closing(base._connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        events, _ = _events(db)
        if len(events) != 1:
            raise ValueError("Audit already started; no reset/retry")
        base._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    jobs = [(kind, symbol, kind + "-" + symbol) for symbol in SYMBOLS for kind in ("spot_meta", "perp_meta")]
    with ThreadPoolExecutor(max_workers=6) as pool:
        receipts = list(pool.map(lambda job: _capture(root, *job), jobs))
    metas, errors = {}, {}
    for symbol in SYMBOLS:
        try:
            data = {v["kind"]: json.loads(v["raw_response_utf8"]) for v in receipts if v["symbol"] == symbol and v["status"] == 200}
            metas[symbol] = metadata(data["spot_meta"], data["perp_meta"], symbol)
        except Exception as error:
            errors[symbol] = type(error).__name__
    if not metas:
        _record(root, "SOURCE_UNAVAILABLE", {"metadata_errors": errors})
        return review(root)
    _record(root, "METADATA", {"markets": metas, "metadata_errors": errors})
    for sample in range(1, SAMPLES + 1):
        if sample > 1:
            time.sleep(5)
        jobs = [(kind, symbol, f"sample-{sample}-" + kind + "-" + symbol)
                for symbol in SYMBOLS for kind in ("spot_book", "perp_book")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda job: _capture(root, *job), jobs))
        values = {}
        for symbol in SYMBOLS:
            if symbol not in metas:
                values[symbol] = {"status": "METADATA_UNAVAILABLE"}
                continue
            sources = {v["kind"]: v for v in rows if v["symbol"] == symbol}
            values[symbol] = analyze_pair(metas[symbol], sources["spot_book"], sources["perp_book"])
        _record(root, "SAMPLE", {"sample": sample, "markets": values})
    return review(root)


def review(root):
    with closing(base._connect(root, readonly=True)) as db:
        events, head = _events(db)
    samples = [v for k, v in events if k == "SAMPLE"]
    return {"engine": ENGINE, "journal_head": head, "registration": events[0][1],
            "metadata": next((v for k, v in events if k == "METADATA"), None),
            "phases": [k for k, _ in events], "samples": samples,
            "complete": len(samples) == SAMPLES, "personal_fee_profile_status": "UNVERIFIED_NOT_QUERIED",
            "actual_fill_costs_measured": False, "execution_authorized": False,
            "profitability_proven": False, "sufficient_to_lower_slippage": False}

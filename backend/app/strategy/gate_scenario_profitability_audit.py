"""Read-only profitability-audit readiness gate for the frozen scenario observer.

Does not simulate fills or invent PnL when the input/execution contract is absent.
No downloads, parameter search, journal mutation, broker or deployed-service hook.
"""
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from math import isfinite
from pathlib import Path
from urllib.parse import urlencode

from app.strategy.gate_scenario_observer import VERSION, Policy
from app.strategy import gate_scenario_observer as frozen_observer


def timestamp(year, month, day):
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


SOURCE_START = timestamp(2023, 11, 1)
EVALUATION_START = timestamp(2023, 11, 15)
END = timestamp(2023, 12, 15)
MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
OBSERVER_SHA256 = "5ca00189023c4b8122577da91f8f41e7b8fc2c06fdc48c6131c63772615c3876"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def expected_sources():
    sources = {}
    for kind in ("candlesticks_1m", "funding_applies"):
        for month in ("202311", "202312"):
            name = f"{kind}-{month}.csv.gz"
            sources[name] = f"https://download.gatedata.org/futures_usdt/{kind}/{month}/BTC_USDT-{month}.csv.gz"
    sources["mark-4h.json"] = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks?" + urlencode({
        "contract": "mark_BTC_USDT", "interval": "4h", "from": SOURCE_START, "to": END + 14400})
    return sources


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean numeric field")
    value = float(value)
    if not isfinite(value):
        raise ValueError("Nonfinite source value")
    return value


def decompress(raw):
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise ValueError("Compressed source budget exceeded")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
        output = f.read(MAX_DECOMPRESSED_BYTES + 1)
    if len(output) > MAX_DECOMPRESSED_BYTES:
        raise ValueError("Decompressed source budget exceeded")
    return output.decode("utf-8-sig")


def parse_prices(raw, month):
    result = []
    for row in csv.reader(io.StringIO(decompress(raw))):
        if not row:
            continue
        if row in (["t", "v", "c", "h", "l", "o"],
                   ["timestamp", "volume", "close", "high", "low", "open"]) and not result:
            continue
        if len(row) != 6:
            raise ValueError("Six-column historical futures K-line schema required")
        t, v, c, h, low, o = map(number, row)
        if (not t.is_integer() or int(t) % 60 or v < 0 or min(c,h,low,o) <= 0
                or h < max(c,low,o) or low > min(c,h,o)
                or datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m") != month):
            raise ValueError("Invalid OHLCV, minute timestamp or month")
        result.append({"t": int(t), "v": v, "c": c, "h": h, "l": low, "o": o})
    if [r["t"] for r in result] != sorted({r["t"] for r in result}):
        raise ValueError("Duplicate or reordered minute source")
    return result


def parse_funding(raw, month):
    result = []
    for row in csv.reader(io.StringIO(decompress(raw))):
        if not row:
            continue
        if row == ["timestamp", "funding_rate"] and not result:
            continue
        if len(row) != 2:
            raise ValueError("Two-column executed-funding schema required")
        t, rate = map(number, row)
        if (not t.is_integer() or int(t) % 28800 or abs(rate) > .1
                or datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m") != month):
            raise ValueError("Invalid executed-funding timestamp/month/rate")
        result.append({"t": int(t), "r": rate})
    if [r["t"] for r in result] != sorted({r["t"] for r in result}):
        raise ValueError("Duplicate or reordered funding source")
    return result


def gap_report(times, *, start, end):
    if (type(start) is not int or type(end) is not int or start >= end
            or start % 60 or end % 60 or end-start > 90*86400
            or any(type(t) is not int or t % 60 for t in times)):
        raise ValueError("Bounded minute window required")
    counts = Counter(t for t in times if start <= t < end)
    duplicates = sum(v-1 for v in counts.values())
    if duplicates:
        raise ValueError("Duplicate source minutes cannot be silently deduplicated")
    missing = sorted(set(range(start, end, 60)) - counts.keys())
    ranges = []
    for t in missing:
        if ranges and t == ranges[-1][1] + 60:
            ranges[-1][1] = t
        else:
            ranges.append([t, t])
    expected = (end-start)//60
    return {"first_open_utc": iso(start), "exclusive_last_open_utc": iso(end),
        "expected_minutes": expected, "observed_minutes": len(counts),
        "missing_minutes": len(missing), "coverage_pct": 100*len(counts)/expected,
        "gap_ranges": [{"first_open_utc": iso(a), "last_open_utc": iso(b),
                        "missing_minutes": (b-a)//60+1} for a,b in ranges],
        "duplicates": duplicates, "imputed_or_forward_filled_minutes": 0}


def context_readiness(times, *, source_start, evaluation_start, end, lookback=80):
    """Only fully observed UTC-aligned bins qualify. Never join across a hole."""
    times = set(times)
    counts, complete, longest = {}, {}, {}
    for name, width in (("15m",900),("1h",3600),("4h",14400)):
        accepted, consecutive, maximum = set(), 0, 0
        for start in range(source_start, end, width):
            if start+width > end:
                break
            if all(t in times for t in range(start, start+width, 60)):
                accepted.add(start+width)  # S1 timestamp is CLOSE, source t is OPEN.
                consecutive += 1
                maximum = max(maximum, consecutive)
            else:
                consecutive = 0
        counts[name] = len(accepted)
        complete[name] = accepted
        longest[name] = maximum
    boundaries = list(range(evaluation_start, end, 900))
    ready = []
    for close in boundaries:
        ready.append(all(all(last-i*width in complete[name] for i in range(lookback))
            for name,width in (("15m",900),("1h",3600),("4h",14400))
            for last in (close//width*width,)))
    return {"aggregation": "Only complete UTC 1m bins; no synthetic candles",
        "lookback_closed_bars_each_timeframe": lookback,
        "complete_aggregate_bars": counts, "longest_consecutive_complete_bars": longest,
        "evaluation_15m_boundaries": len(boundaries),
        "boundaries_with_complete_15m_1h_4h_context": sum(ready),
        "boundaries_without_complete_context": len(boundaries)-sum(ready),
        "missing_minute_cause_verified": False}


def audit(root):
    root = Path(root)
    if hashlib.sha256(Path(frozen_observer.__file__).read_bytes()).hexdigest() != OBSERVER_SHA256:
        raise ValueError("Audit requires the frozen PR99 observer; review changed implementations separately")
    intent_path = root/"source-intent.json"
    intent = json.loads(intent_path.read_text())
    if (intent["fixed_market"] != "BTC_USDT" or intent["price_source_months"] != ["202311","202312"]
            or intent["fixed_evaluation_start"] != "2023-11-15T00:00:00Z"
            or intent["fixed_evaluation_end"] != "2023-12-15T00:00:00Z"
            or intent["warmup_start"] != "2023-11-01T00:00:00Z"
            or intent["source_get_budget"] != 5 or intent["execution_authorized"] is not False):
        raise ValueError("Preserved source-intent contract required")
    hashes, receipts, prices, funding, mark = {}, [], [], [], None
    declared = datetime.fromisoformat(intent["declared_at_utc"])
    for name, url in expected_sources().items():
        path = root/"raw"/name
        receipt_path = path.with_name(path.name+".receipt.json")
        raw, receipt = path.read_bytes(), json.loads(receipt_path.read_text())
        sha = hashlib.sha256(raw).hexdigest()
        if (receipt["url"] != url or receipt["status"] != 200 or sha != receipt["sha256"]
                or len(raw) != receipt["bytes"] or len(raw) > MAX_COMPRESSED_BYTES
                or not declared <= datetime.fromisoformat(receipt["requested_at_utc"])
                       <= datetime.fromisoformat(receipt["received_at_utc"])):
            raise ValueError("Public source receipt/hash/clock mismatch")
        hashes[str(path.relative_to(root))] = sha
        hashes[str(receipt_path.relative_to(root))] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        receipts.append({"file": name, "bytes": len(raw), "status": receipt["status"]})
        if name.startswith("candlesticks_"):
            prices.extend(parse_prices(raw, name.split("-")[1][:6]))
        elif name.startswith("funding_"):
            funding.extend(parse_funding(raw, name.split("-")[1][:6]))
        else:
            mark = json.loads(raw)
    times = [r["t"] for r in prices if SOURCE_START <= r["t"] < END]
    rate_times = [r["t"] for r in funding if SOURCE_START <= r["t"] <= END]
    if rate_times != list(range(SOURCE_START, END+1, 28800)):
        raise ValueError("Executed-funding coverage mismatch")
    if not isinstance(mark,list) or [r["t"] for r in mark] != list(range(SOURCE_START, END+14401, 14400)):
        raise ValueError("Mark proxy coverage mismatch")
    for r in mark:
        o,h,low,c = (number(r[k]) for k in ("o","h","l","c"))
        if min(o,h,low,c)<=0 or h<max(o,low,c) or low>min(o,h,c):
            raise ValueError("Invalid mark proxy OHLC")
    whole = gap_report(times,start=SOURCE_START,end=END)
    evaluation = gap_report(times,start=EVALUATION_START,end=END)
    context = context_readiness(times,source_start=SOURCE_START,evaluation_start=EVALUATION_START,end=END)
    blockers = ["OBSERVER_HAS_NO_COMPLETE_TRADE_ENTRY_POSITION_EXIT_CONTRACT",
                "NO_REAL_MARKET_CLOSED_TRADES_FOR_THIS_OBSERVER_AUDITED"]
    if whole["missing_minutes"]:
        blockers.append("HISTORICAL_MINUTE_GRID_INCOMPLETE")
    if context["boundaries_with_complete_15m_1h_4h_context"] == 0:
        blockers.append("NO_FULL_S1_CONTEXT_FROM_STRICT_MINUTE_AGGREGATION")
    return {"audit": "SCENARIO_PROFITABILITY_READINESS_AUDIT_V1",
        "status": "INCONCLUSIVE_NOT_FINANCIALLY_EVALUATED", "blockers": blockers,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "observer_version": VERSION, "observer_commit": intent["parent_observer_commit"],
        "observer_source_sha256": OBSERVER_SHA256,
        "source_intent_sha256_unchanged": hashlib.sha256(intent_path.read_bytes()).hexdigest(),
        "archived_file_sha256": hashes, "verified_public_receipts": receipts,
        "source_collection_after_declared_intent_verified": True,
        "full_source_month_rows_before_fixed_window_slice": len(prices),
        "full_window": whole, "evaluation_window": evaluation, "technical_context": context,
        "executed_funding_rates_in_window": len(rate_times), "mark_4h_proxy_rows": len(mark),
        "observer_cost_assumptions_bps_side": {"fee":Policy().fee_bps_side,"slippage":Policy().slippage_bps_side},
        "observer_cost_assumptions_are_personal_verified_rates": False,
        "observer_candidate_is_filled_trade": False,
        "execution_contract_missing": ["sizing_and_historical_lots","entry_fill_and_revalidation_latency",
            "position_and_stop_target_exit_engine","maximum_holding_period","funding_cashflows",
            "margin_liquidation_and_account_risk_integration"],
        "profitability_metrics": {k:{"status":"NOT_COMPUTED","value":None} for k in
            ("net_pnl","win_rate","profit_factor","max_drawdown","expectancy")},
        "new_financial_replays":0,"new_significance_tests":0,
        "existing_holdout_reserve_financially_evaluated":False,
        "globally_unseen_project_history_certified":False,
        "strategy_declared_unprofitable":False,"profitability_proven":False,
        "eligible_for_s5":False,"execution_authorized":False}

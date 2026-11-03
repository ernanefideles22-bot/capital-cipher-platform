"""Preregistered funding-rate forecast triage, before any paired-price replay.

Normalized funding-rate sums are not trade PnL. Existing economics, S3 and risk
modules stay frozen. No fit, parameter search, auto-promotion or order API.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from app.strategy import gate_s4_funding_feasibility as base

ENGINE = "S4_BTC_PAST_ONLY_FUNDING_COST_FORECAST_V1"
DAY, INTERVAL = 86400, 28800
FIRST = int(datetime(2022, 12, 1, tzinfo=timezone.utc).timestamp())
DECISION_START = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp())
END = DECISION_START + 360 * DAY
MONTHS = ("202212",) + tuple(f"2023{m:02}" for m in range(1, 13))
PUBLICATION_LAG = 3600  # Assumption, not verified historical as-of receipts.
PAYMENTS = 88  # Entry +8h; settlements strictly inside the following 30 days.
THRESHOLD_BPS = 132.  # 112 four-fill stress costs + 20 basis uncertainty budget.


def rules():
    return {"engine": ENGINE, "symbol": "BTC_USDT", "months": list(MONTHS),
            "source_first": FIRST, "source_end": END,
            "decision_first": DECISION_START, "decision_count": 12,
            "decision_spacing_days": 30, "entry_delay_seconds": 28800,
            "exit_after_decision_days": 30, "funding_grid_seconds": INTERVAL,
            "publication_lag_assumption_seconds": PUBLICATION_LAG,
            "historical_publication_receipts_verified": False,
            "prediction": "88 * min(mean settled rate in past 7d, mean in past 30d)",
            "history_window": "decision - duration <= settlement; settlement + 1h <= decision",
            "expected_events_7d_30d": [21, 90],
            "projection_is_heuristic_not_confidence_bound": True,
            "rate_sum_projection_not_fixed_quantity_cashflow": True,
            "benchmark": "88 * constant 0.0001 per settlement; not fitted",
            "secondary_reference": "88 * past 30d mean; descriptive, not selected",
            "entry_screen_bps": THRESHOLD_BPS,
            "nominal_four_fill_cost_bps": 76, "stress_four_fill_cost_bps": 112,
            "basis_uncertainty_budget_bps": 20,
            "costs_are_fixed_notional_planning_assumptions": True,
            "cost_parameters": base.SCENARIOS,
            "screen_only": {"minimum_eligible_blocks": 6,
                "every_eligible_realized_rate_sum_at_least_132bps": True,
                "mse_strictly_below_constant_benchmark": True},
            "candidate_count": 1, "parameter_search_budget": 0,
            "source_attempt_budget": 1, "public_request_budget": 13,
            "data_choice": "full 2023 first 360 days, before prior 2024 BTC study; 31d warmup",
            "existing_reserve_evaluated": False, "prospective_validation": False,
            "globally_unseen_project_history_certified": False,
            "post_hoc_derived_hypothesis": True,
            "previous_pr": 96, "previous_commit": "327721d5c71e8d4bd3289dbb654eefc6f09ed6d5",
            "previous_report": "libfile_e8b6be6ff618819190cd8c375e104579",
            "financial_replays_budget_this_stage": 0,
            "statistical_significance_tests_budget": 0,
            "price_basis_margin_liquidity_and_fills_modelled": False,
            "profitability_proven": False, "eligible_for_s5": False,
            "execution_authorized": False, "automatic_promotion_authorized": False}


def fingerprint():
    return base.digest({"forecast_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        "base_source": base.fingerprint()})


def _events(db):
    result, previous = [], base.GENESIS
    for seq, kind, payload, prev, sha in db.execute("SELECT * FROM events ORDER BY seq"):
        if seq != len(result) + 1 or prev != previous or sha != base.digest([prev, kind, payload]):
            raise ValueError("Forecast journal chain mismatch")
        result.append((kind, json.loads(payload)))
        previous = sha
    kinds = [k for k, _ in result]
    if kinds not in (["REGISTERED"], ["REGISTERED", "STARTED"],
            ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"],
            ["REGISTERED", "STARTED", "SOURCE_AVAILABLE"],
            ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "RESULT"],
            ["REGISTERED", "STARTED", "SOURCE_AVAILABLE", "FAILED"]):
        raise ValueError("Illegal forecast phase or budget")
    if result[0][1]["rules"] != rules() or result[0][1]["source_sha256"] != fingerprint():
        raise ValueError("Frozen forecast rules/source changed")
    return result, previous


def register(root):
    p = base._path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("xb"):
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


def archive_get(month):
    if month not in MONTHS:
        raise ValueError("Only registered BTC executed-funding archive GETs")
    url = f"https://download.gatedata.org/futures_usdt/funding_applies/{month}/BTC_USDT-{month}.csv.gz"
    receipt = {"month": month, "url": url, "requested_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        with build_opener(base._NoRedirect()).open(Request(url, method="GET"), timeout=30) as response:
            if response.url != url or response.status != 200:
                raise ValueError("Unexpected archive redirect/status")
            raw, status = response.read(2 * 1024 * 1024 + 1), 200
    except HTTPError as error:
        raw, status = error.read(2 * 1024 * 1024 + 1), error.code
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Compressed archive byte budget")
    receipt.update(status=status, received_at_utc=datetime.now(timezone.utc).isoformat(),
                   response_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    return receipt, raw


def parse_archive(raw, month):
    if month not in MONTHS:
        raise ValueError("Unregistered archive month")
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
        content = f.read(8 * 1024 * 1024 + 1)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Decompressed archive byte budget")
    result, header_seen = [], False
    for row in csv.reader(io.StringIO(content.decode("utf-8-sig"))):
        if not row or all(not c.strip() for c in row):
            continue
        if [c.strip().lower() for c in row] == ["timestamp", "funding_rate"]:
            if result or header_seen:
                raise ValueError("Repeated/misplaced archive header")
            header_seen = True
            continue
        if len(row) != 2:
            raise ValueError("Executed funding two-column schema required")
        t, rate = map(base._number, row)
        if (not t.is_integer() or abs(rate) > .1 or
                datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m") != month):
            raise ValueError("Funding month/timestamp/rate mismatch")
        if FIRST <= t <= END:
            result.append({"t": int(t), "r": rate})
    return result


def validate(rows):
    if not isinstance(rows, list) or [r["t"] for r in rows] != list(range(FIRST, END + 1, INTERVAL)):
        raise ValueError("Full regular funding grid required; no imputation")
    if any(type(r["t"]) is not int or type(r["r"]) not in (int, float) or
           not math.isfinite(r["r"]) or abs(r["r"]) > .1 for r in rows):
        raise ValueError("Nonfinite/invalid timestamp or funding rate")


def capture(root):
    folder = Path(root) / "source"
    folder.mkdir(exist_ok=False)
    rows = []
    for month in MONTHS:
        receipt, raw = archive_get(month)
        with (folder / f"{month}.csv.gz").open("xb") as f:
            f.write(raw)
        with (folder / f"{month}.json").open("x") as f:
            json.dump(receipt, f, indent=2)
        if receipt["status"] != 200:
            raise ValueError(f"ARCHIVE_HTTP_{receipt['status']}_{month}")
        rows.extend(parse_archive(raw, month))
    rows.sort(key=lambda r: r["t"])
    validate(rows)
    return rows


def predict(history, decision):
    """As-of interface refuses unavailable records instead of silently ignoring."""
    if len(history) != 90:
        raise ValueError("Exactly 30 days of as-of rates required")
    if any(r["t"] + PUBLICATION_LAG > decision for r in history):
        raise ValueError("Future or not-yet-available funding in predictor")
    means = []
    for days, count in ((7, 21), (30, 90)):
        relevant = [r for r in history if decision - days * DAY <= r["t"]]
        expected = list(range(decision - days * DAY, decision, INTERVAL))
        if len(relevant) != count or [r["t"] for r in relevant] != expected:
            raise ValueError("Missing/stale/duplicate as-of history")
        if any(type(r["r"]) not in (int, float) or not math.isfinite(r["r"]) or abs(r["r"]) > .1 for r in relevant):
            raise ValueError("Invalid past rate")
        means.append(fmean(r["r"] for r in relevant))
    value = PAYMENTS * min(means) * 10000
    return {"decision_t": decision, "last_rate_t": history[-1]["t"],
            "assumed_last_available_t": history[-1]["t"] + PUBLICATION_LAG,
            "mean_7d": means[0], "mean_30d": means[1], "forecast_bps": value,
            "constant_forecast_bps": PAYMENTS,
            "mean30_forecast_bps": PAYMENTS * means[1] * 10000,
            "eligible_by_known_rates": value >= THRESHOLD_BPS}


def evaluate(rows):
    validate(rows)
    decisions = []
    for b in range(12):
        t = DECISION_START + b * 30 * DAY
        # Pass the predictor a strictly truncated input, never the outcome series.
        past = [r for r in rows if t - 30 * DAY <= r["t"] and r["t"] + PUBLICATION_LAG <= t]
        prediction = predict(past, t)
        entry, exit_t = t + INTERVAL, t + 30 * DAY
        future = [r for r in rows if entry < r["t"] < exit_t]
        if len(future) != PAYMENTS:
            raise ValueError("Outcome window/count mismatch")
        actual = sum(r["r"] for r in future) * 10000
        decisions.append({"block": b + 1, **prediction, "entry_t": entry, "exit_t": exit_t,
            "outcome_first_t": future[0]["t"], "outcome_last_t": future[-1]["t"],
            "realized_rate_sum_bps": actual, "realized_events": len(future),
            "realized_rate_sum_covers_planning_threshold": actual >= THRESHOLD_BPS,
            "forecast_error_bps": prediction["forecast_bps"] - actual})
    mse = fmean(r["forecast_error_bps"] ** 2 for r in decisions)
    baseline_mse = fmean((r["constant_forecast_bps"] - r["realized_rate_sum_bps"]) ** 2 for r in decisions)
    mean30_mse = fmean((r["mean30_forecast_bps"] - r["realized_rate_sum_bps"]) ** 2 for r in decisions)
    eligible = [r for r in decisions if r["eligible_by_known_rates"]]
    reasons = []
    if len(eligible) < 6:
        reasons.append("FEWER_THAN_SIX_ELIGIBLE_BLOCKS")
    if any(not r["realized_rate_sum_covers_planning_threshold"] for r in eligible):
        reasons.append("ELIGIBLE_FORECAST_FAILED_RATE_COVERAGE")
    if not mse < baseline_mse:
        reasons.append("FORECAST_NOT_BETTER_THAN_CONSTANT_BENCHMARK")
    return {"decisions": decisions, "forecast_mse_bps_squared": mse,
            "constant_benchmark_mse_bps_squared": baseline_mse,
            "mean30_reference_mse_bps_squared": mean30_mse,
            "mse_skill_vs_constant": 1 - mse / baseline_mse if baseline_mse > 0 else None,
            "eligible_blocks": len(eligible),
            "eligible_rate_coverage_failures": sum(not r["realized_rate_sum_covers_planning_threshold"] for r in eligible),
            "eligible_for_later_pair_replay_only": not reasons, "reasons": reasons,
            "status": "FORECAST_TRIAGE_REJECTED" if reasons else "FORECAST_ONLY_REQUIRES_PAIR_REPLAY",
            "cash_abstention_not_a_successful_trade": True,
            "rates_not_trade_pnl": True, "financial_replays_executed": 0,
            "statistical_tests_executed": 0, "independence_certified": False,
            "profitability_proven": False, "eligible_for_s5": False, "execution_authorized": False}


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
            raise ValueError("Already started; no retry/reset or selective replacement")
        base._append(db, "STARTED", {"at_utc": datetime.now(timezone.utc).isoformat()})
    try:
        rows = capture(root)
        validate(rows)
        with (Path(root) / "funding-series.json").open("x") as f:
            f.write(base.canonical(rows))
        _record(root, "SOURCE_AVAILABLE", {"records": len(rows), "raw_archives": 13,
                "data_sha256": base.digest(rows)})
    except Exception as exc:
        _record(root, "SOURCE_UNAVAILABLE", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
        return review(root)
    try:
        result = evaluate(rows)
    except Exception as exc:
        _record(root, "FAILED", {"error_type": type(exc).__name__, "reason": str(exc)[:300]})
    else:
        _record(root, "RESULT", result)
    return review(root)


def review(root):
    with closing(base._connect(root, readonly=True)) as db:
        values, head = _events(db)
    return {"engine": ENGINE, "journal_head": head, "phases": [k for k, _ in values],
            "registration": values[0][1], "source": next((v for k, v in values if k.startswith("SOURCE_")), None),
            "result": next((v for k, v in values if k == "RESULT"), None),
            "profitability_proven": False, "execution_authorized": False, "eligible_for_s5": False}

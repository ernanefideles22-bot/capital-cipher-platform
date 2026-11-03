import copy
import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.strategy import gate_s4_funding_forecast as s


def fixture(rate=.0002):
    return [{"t": t, "r": rate} for t in range(s.FIRST, s.END + 1, s.INTERVAL)]


def past(rows, t=s.DECISION_START):
    return [r for r in rows if t - 30 * s.DAY <= r["t"] and r["t"] + 3600 <= t]


def test_projection_units_and_exact_88_outcome_clock_oracle():
    result = s.evaluate(fixture())
    d = result["decisions"][0]
    assert d["forecast_bps"] == pytest.approx(176)
    assert d["realized_rate_sum_bps"] == pytest.approx(176)
    assert d["realized_events"] == 88
    assert d["last_rate_t"] == s.DECISION_START - 28800
    assert d["assumed_last_available_t"] < d["decision_t"] < d["entry_t"] < d["outcome_first_t"]
    assert d["outcome_last_t"] < d["exit_t"]
    assert result["forecast_mse_bps_squared"] == pytest.approx(0)
    assert result["constant_benchmark_mse_bps_squared"] == pytest.approx(88 ** 2)
    assert result["mse_skill_vs_constant"] == pytest.approx(1)


def test_a_passing_rate_screen_still_cannot_promote_or_claim_pnl():
    r = s.evaluate(fixture())
    assert r["eligible_blocks"] == 12 and r["eligible_for_later_pair_replay_only"]
    assert r["rates_not_trade_pnl"] and r["financial_replays_executed"] == 0
    assert r["statistical_tests_executed"] == 0
    assert not r["profitability_proven"] and not r["eligible_for_s5"] and not r["execution_authorized"]


def test_minimum_past_window_forecast_is_not_selected_from_future():
    rows = past(fixture(.0003))
    for r in rows[-21:]:
        r["r"] = .0001
    result = s.predict(rows, s.DECISION_START)
    assert result["forecast_bps"] == pytest.approx(88)
    assert result["mean30_forecast_bps"] > result["forecast_bps"]
    assert not result["eligible_by_known_rates"]


def test_future_reversal_cannot_rewrite_first_decision():
    rows = fixture()
    changed = copy.deepcopy(rows)
    for r in changed:
        if r["t"] >= s.DECISION_START:
            r["r"] = -.001
    a, b = (s.evaluate(r)["decisions"][0] for r in (rows, changed))
    for k in ("forecast_bps", "eligible_by_known_rates", "last_rate_t", "entry_t", "exit_t"):
        assert a[k] == b[k]
    assert b["realized_rate_sum_bps"] < 0 < a["realized_rate_sum_bps"]


def test_eligible_forecast_failure_is_retained_not_dropped():
    rows = fixture()
    for r in rows:
        if r["t"] >= s.DECISION_START:
            r["r"] = 0
    result = s.evaluate(rows)
    assert result["decisions"][0]["eligible_by_known_rates"]
    assert result["eligible_rate_coverage_failures"] >= 1
    assert "ELIGIBLE_FORECAST_FAILED_RATE_COVERAGE" in result["reasons"]
    assert not result["eligible_for_later_pair_replay_only"]


def test_zero_trades_is_abstention_not_profitable_validation():
    result = s.evaluate(fixture(-.0001))
    assert result["eligible_blocks"] == 0 and result["eligible_rate_coverage_failures"] == 0
    assert "FEWER_THAN_SIX_ELIGIBLE_BLOCKS" in result["reasons"]
    assert not result["eligible_for_later_pair_replay_only"]


def test_degenerate_baseline_cannot_invent_skill():
    result = s.evaluate(fixture(.0001))
    assert result["mse_skill_vs_constant"] is None
    assert "FORECAST_NOT_BETTER_THAN_CONSTANT_BENCHMARK" in result["reasons"]


@pytest.mark.parametrize("damage", ["future", "stale", "duplicate", "nan", "extra_old"])
def test_asof_history_rejects_bad_availability_or_grid(damage):
    rows = past(fixture())
    if damage == "future":
        rows[-1]["t"] = s.DECISION_START
    elif damage == "stale":
        rows.pop()
    elif damage == "duplicate":
        rows[-1] = rows[-2]
    elif damage == "nan":
        rows[-1]["r"] = float("nan")
    else:
        rows.insert(0, {"t": s.FIRST, "r": .0002})
    with pytest.raises(ValueError):
        s.predict(rows, s.DECISION_START)


def test_decision_boundary_and_exit_funding_do_not_leak_into_first_outcome():
    a = fixture()
    b = copy.deepcopy(a)
    for r in b:
        if r["t"] in (s.DECISION_START, s.DECISION_START + 28800, s.DECISION_START + 30 * s.DAY):
            r["r"] = .05
    ra, rb = (s.evaluate(r)["decisions"][0] for r in (a, b))
    assert ra["forecast_bps"] == rb["forecast_bps"]
    assert ra["realized_rate_sum_bps"] == rb["realized_rate_sum_bps"]


def test_global_grid_gaps_duplicates_nonfinite_fail_closed():
    for kind in ("gap", "duplicate", "nan"):
        rows = fixture()
        if kind == "gap":
            del rows[5]
        elif kind == "duplicate":
            rows[5] = rows[4]
        else:
            rows[5]["r"] = float("nan")
        with pytest.raises(ValueError):
            s.evaluate(rows)


def test_csv_month_mapping_and_unscored_month_tail():
    raw = gzip.compress(f"timestamp,funding_rate\n{s.FIRST},-0.0002\n".encode())
    assert s.parse_archive(raw, "202212") == [{"t": s.FIRST, "r": -.0002}]
    tail = gzip.compress(f"{s.END},0.0001\n{s.END + 28800},0.09\n".encode())
    assert s.parse_archive(tail, "202312") == [{"t": s.END, "r": .0001}]
    with pytest.raises(ValueError):
        s.parse_archive(raw, "202301")


def test_outside_archive_allowlist_rejected_before_network():
    with patch.object(s, "build_opener", side_effect=AssertionError("network forbidden")):
        with pytest.raises(ValueError):
            s.archive_get("202404")


def test_capture_exact_public_budget_and_funding_mapping(tmp_path):
    rows = fixture()
    def get(month):
        values = [r for r in rows if datetime.fromtimestamp(r["t"], timezone.utc).strftime("%Y%m") == month]
        raw = gzip.compress("\n".join(f"{r['t']},{r['r']}" for r in values).encode())
        return {"status": 200, "response_sha256": hashlib.sha256(raw).hexdigest()}, raw
    with patch.object(s, "archive_get", side_effect=get) as request:
        captured = s.capture(tmp_path)
    assert captured == rows and request.call_count == 13
    assert len(list((tmp_path / "source").glob("*.json"))) == 13


def test_exclusive_registration_appendonly_and_rule_fingerprint(tmp_path):
    m = s.register(tmp_path)
    assert m["rules"]["financial_replays_budget_this_stage"] == 0
    with pytest.raises(FileExistsError):
        s.register(tmp_path)
    with closing(s.base._connect(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE events SET kind='fake'")
    with patch.object(s, "fingerprint", return_value="changed"):
        with pytest.raises(ValueError):
            s.review(tmp_path)


def test_source_failure_preserves_receipt_and_cannot_retry(tmp_path):
    s.register(tmp_path)
    with patch.object(s, "archive_get", return_value=({"status": 404}, b"unavailable")) as get:
        result = s.run(tmp_path)
    assert get.call_count == 1 and result["phases"][-1] == "SOURCE_UNAVAILABLE"
    assert result["result"] is None
    assert (tmp_path / "source/202212.csv.gz").read_bytes() == b"unavailable"
    with pytest.raises(ValueError):
        s.run(tmp_path)


def test_success_journal_datahash_and_no_retry(tmp_path):
    s.register(tmp_path)
    with patch.object(s, "capture", return_value=fixture()):
        result = s.run(tmp_path)
    assert result["phases"][-1] == "RESULT"
    assert result["source"]["data_sha256"] == hashlib.sha256((tmp_path / "funding-series.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        s.run(tmp_path)

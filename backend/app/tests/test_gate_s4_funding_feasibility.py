import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from app.strategy import gate_s4_funding_feasibility as s


def fixture(rate=.0002):
    prices = [{"t": t, "o": 100., "h": 102., "l": 98., "c": 100.}
              for t in range(s.START, s.END + 1, s.STEP)]
    return {"spot": copy.deepcopy(prices), "perp": copy.deepcopy(prices),
            "mark": copy.deepcopy(prices),
            "funding": [{"t": t, "r": rate} for t in range(s.START, s.END + 1, 28800)]}


def test_equal_units_four_fills_and_funding_oracle():
    v = s.evaluate_block(fixture(), 0, "nominal")
    q = 400 / 100.06
    # Independent constant-price oracle: two adverse buys/sells per leg.
    assert v["quantity"] == pytest.approx(q)
    assert v["funding_events"] == 89
    assert v["funding_point_proxy"] == pytest.approx(q * 100 * .0002 * 89)
    expected_fees = q * ((100.06 + 99.94) * .002 + (99.94 + 100.06) * .0006)
    assert v["fees"] == pytest.approx(expected_fees)
    assert v["slippage_cost"] == pytest.approx(q * .24)
    assert v["net_point_proxy"] == pytest.approx(q * 100 * .0002 * 89 - expected_fees - q * .24)
    assert v["equity_final_point_proxy"] == pytest.approx(1000 + v["net_point_proxy"])


def test_negative_funding_is_short_debit_with_reversed_bounds():
    v = s.evaluate_block(fixture(-.0002), 0, "nominal")
    assert v["funding_negative"] == 89 and v["funding_point_proxy"] < 0
    assert v["funding_lower_bound"] < v["funding_point_proxy"] < v["funding_upper_bound"]
    assert v["net_point_proxy"] < v["net_without_funding_same_path"]


def test_funding_bound_uses_both_sides_of_settlement_boundary():
    mark = {"o": 100, "l": 99, "h": 101}
    before = {"o": 100, "l": 90, "h": 110}
    assert s._payment(2, .001, mark, before) == pytest.approx((.2, .18, .22))
    assert s._payment(2, -.001, mark, before) == pytest.approx((-.2, -.22, -.18))


def test_entry_exit_settlement_excluded_and_delay_is_fixed():
    data = fixture()
    for r in data["funding"]:
        if r["t"] in (s.START, s.START + 30 * s.DAY):
            r["r"] = .05
    a = s.evaluate_block(data, 0, "nominal")
    b = s.evaluate_block(data, 0, "stress_delayed")
    assert a["entry_t"] == s.START + s.STEP
    assert b["entry_t"] == s.START + 2 * s.STEP
    assert b["funding_events"] == 88
    assert a["funding_rate_sum_bps"] == pytest.approx(.0002 * 89 * 10000)


def test_future_changes_cannot_choose_entry_or_quantity():
    a = fixture()
    b = copy.deepcopy(a)
    for r in b["funding"]:
        r["r"] *= -1
    for r in b["spot"][20:]:
        r.update(o=200, c=200, h=201, l=199)
    va, vb = (s.evaluate_block(d, 0, "nominal") for d in (a, b))
    assert va["entry_t"] == vb["entry_t"] and va["quantity"] == vb["quantity"]


def test_basis_pnl_is_separate_and_directional_moves_cancel():
    a = fixture(0)
    exit_t = s.START + 30 * s.DAY
    for k in ("spot", "perp", "mark"):
        row = next(r for r in a[k] if r["t"] == exit_t)
        row.update(o=120, c=120, h=122, l=118)
    v = s.evaluate_block(a, 0, "nominal")
    assert v["basis_price_pnl_before_slippage"] == pytest.approx(0)
    assert v["net_point_proxy"] < 0
    a["perp"][180]["o"] = 119
    w = s.evaluate_block(a, 0, "nominal")
    assert w["basis_price_pnl_before_slippage"] == pytest.approx(w["quantity"])


def test_short_leg_margin_exhaustion_not_hidden_by_spot_gain():
    data = fixture()
    for k in ("spot", "perp", "mark"):
        data[k][2].update(o=210, c=210, h=220, l=200)
    v = s.evaluate_block(data, 0, "nominal")
    assert v["minimum_short_margin_buffer_proxy"] < 0
    assert v["short_leg_liquidation_certified_absent"] is False


def test_six_blocks_are_not_eighteen_independent_periods_or_approved_profit():
    result = s.evaluate(fixture())
    assert len(result["rows"]) == 18 and result["distinct_calendar_blocks"] == 6
    assert result["economic_screen_passed"] is True
    assert result["profitability_proven"] is False and result["eligible_for_s5"] is False
    assert result["execution_authorized"] is False and result["statistical_tests_executed"] == 0
    assert len({r["quantity"] for r in result["rows"]}) == 2


def test_one_failed_scenario_is_not_rescued_by_nominal_profit():
    result = s.evaluate(fixture(.0001))
    assert result["economic_screen_passed"] is False
    assert any("stress_NONPOSITIVE_NET" in reason for reason in result["reasons"])
    assert any(r["net_point_proxy"] > 0 for r in result["rows"] if r["scenario"] == "nominal")


@pytest.mark.parametrize("damage", ["gap", "duplicate", "nan", "bad_ohlc", "future", "funding_gap"])
def test_corrupt_or_misaligned_data_fails_closed(damage):
    data = fixture()
    if damage == "gap":
        del data["spot"][2]
    elif damage == "duplicate":
        data["mark"][2] = data["mark"][1]
    elif damage == "nan":
        data["perp"][2]["c"] = float("nan")
    elif damage == "bad_ohlc":
        data["spot"][2]["h"] = 90
    elif damage == "future":
        data["spot"][-1]["t"] += s.STEP
    else:
        del data["funding"][2]
    with pytest.raises(ValueError):
        s.evaluate(data)


def test_raw_spot_field_mapping_and_closed_flag():
    raw = [[s.START, "17", "101", "103", "98", "100", "2", "true"]]
    assert s.normalize("spot", raw, s.START, s.START) == [
        {"t": s.START, "o": 100, "h": 103, "l": 98, "c": 101}]
    raw[0][-1] = "false"
    with pytest.raises(ValueError):
        s.normalize("spot", raw, s.START, s.START)


@pytest.mark.parametrize("kind,lower,upper", [("orders", s.START, s.END), ("spot", s.START - 1, s.END),
                                                    ("perp", s.START, s.END + s.STEP)])
def test_public_whitelist_and_period_before_network(kind, lower, upper):
    with patch.object(s, "build_opener", side_effect=AssertionError("network forbidden")):
        with pytest.raises(ValueError):
            s.public_get(kind, lower, upper)


def test_capture_exact_budget_and_roundtrip_receipts(tmp_path):
    data = fixture()
    def get(kind, lower, upper):
        rows = [r for r in data[kind] if lower <= r["t"] <= upper]
        raw = ([[r["t"], 1, r["c"], r["h"], r["l"], r["o"], 1, "true"] for r in rows]
               if kind == "spot" else rows)
        return {"status": 200, "raw_response_utf8": json.dumps(raw)}
    with patch.object(s, "public_get", side_effect=get) as request:
        captured = s.capture(tmp_path)
    assert captured == data and request.call_count == 16
    assert len(list((tmp_path / "source").iterdir())) == 16


def test_registration_exclusive_source_rules_frozen_and_append_only(tmp_path):
    m = s.register(tmp_path)
    assert m["rules"]["existing_reserve_evaluated"] is False
    with pytest.raises(FileExistsError):
        s.register(tmp_path)
    with closing(s._connect(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM events")
    with patch.object(s, "fingerprint", return_value="different"):
        with pytest.raises(ValueError):
            s.review(tmp_path)


def test_http_failure_receipt_preserved_without_retry(tmp_path):
    s.register(tmp_path)
    receipt = {"status": 400, "raw_response_utf8": '{"message":"no history"}'}
    with patch.object(s, "public_get", return_value=receipt) as get:
        result = s.run(tmp_path)
    assert get.call_count == 1
    assert result["phases"] == ["REGISTERED", "STARTED", "SOURCE_UNAVAILABLE"]
    assert result["result"] is None
    assert json.loads((tmp_path / "source/funding-01.json").read_text()) == receipt
    with pytest.raises(ValueError):
        s.run(tmp_path)


def test_run_success_preserves_data_hash_and_prohibits_retry(tmp_path):
    s.register(tmp_path)
    with patch.object(s, "capture", return_value=fixture()):
        result = s.run(tmp_path)
    assert result["phases"][-1] == "RESULT"
    assert result["source"]["data_sha256"] == hashlib.sha256((tmp_path / "normalized-series.json").read_bytes()).hexdigest()
    assert result["profitability_proven"] is False
    with pytest.raises(ValueError):
        s.run(tmp_path)


def test_symlink_journal_root_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    fake = tmp_path / "fake"
    fake.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        s.register(fake)

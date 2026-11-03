import copy
import gzip
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.strategy import gate_s4_funding_archive_v2 as a
from app.strategy import gate_s4_funding_feasibility as s
from app.tests.test_gate_s4_funding_feasibility import fixture


def failed_parent(root, message="from time exceeds 180-day limit"):
    s.register(root)
    raw = json.dumps({"label": "INVALID_PARAM_VALUE", "message": message})
    receipt = {"kind": "funding", "status": 400, "raw_response_utf8": raw,
               "response_sha256": hashlib.sha256(raw.encode()).hexdigest()}
    with patch.object(s, "public_get", return_value=receipt):
        s.run(root)


def csv_bytes(month, data=None):
    data = data or fixture()
    rows = [r for r in data["funding"]
            if datetime.fromtimestamp(r["t"], timezone.utc).strftime("%Y%m") == month]
    text = "timestamp,funding_rate\n" + "\n".join(f"{r['t']},{r['r']}" for r in rows)
    return gzip.compress(text.encode())


def test_retention_adaptation_preserves_prior_bytes_and_identical_economics(tmp_path):
    prior, new = tmp_path / "v1", tmp_path / "v2"
    failed_parent(prior)
    before = {str(p.relative_to(prior)): p.read_bytes() for p in prior.rglob("*") if p.is_file()}
    m = a.register(new, prior)
    assert m["rules"]["paired_economics_v1"] == s.manifest()
    assert m["no_prior_financial_results"] is True
    assert before == {str(p.relative_to(prior)): p.read_bytes() for p in prior.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        a.register(new, prior)


def test_other_source_failures_do_not_authorize_archive_adaptation(tmp_path):
    failed_parent(tmp_path / "v1", message="arbitrary error")
    with pytest.raises(ValueError):
        a.register(tmp_path / "v2", tmp_path / "v1")
    assert not (tmp_path / "v2").exists()


def test_modified_raw_failure_not_accepted(tmp_path):
    failed_parent(tmp_path / "v1")
    p = tmp_path / "v1/source/funding-01.json"
    r = json.loads(p.read_text())
    r["raw_response_utf8"] += " "
    p.write_text(json.dumps(r))
    with pytest.raises(ValueError):
        a.register(tmp_path / "v2", tmp_path / "v1")


def test_official_csv_order_sign_and_fixed_calendar_filter():
    raw = csv_bytes("202404", fixture(-.0002))
    rows = a.parse_archive(raw, "202404")
    assert len(rows) == 90 and rows[0] == {"t": s.START, "r": -.0002}
    # A monthly file may include unscored rows beyond the exact end boundary.
    text = f"{s.END},0.0001\n{s.END + 28800},0.09\n"
    assert a.parse_archive(gzip.compress(text.encode()), "202409") == [{"t": s.END, "r": .0001}]


@pytest.mark.parametrize("raw", [b"bad gzip", gzip.compress(b"timestamp,rate,extra\n"),
                                      gzip.compress(b"1711929600,nan\n"),
                                      gzip.compress(b"1711929601.1,0.01\n")])
def test_corrupt_archive_fails_closed(raw):
    with pytest.raises((ValueError, OSError)):
        a.parse_archive(raw, "202404")


def test_unregistered_month_rejected_before_network():
    with patch.object(a, "build_opener", side_effect=AssertionError("network forbidden")):
        with pytest.raises(ValueError):
            a.archive_get("202610")


def test_archive_capture_has_exact_frozen_source_budget(tmp_path):
    data = fixture()
    def get_archive(month):
        raw = csv_bytes(month, data)
        return {"status": 200, "response_sha256": hashlib.sha256(raw).hexdigest()}, raw
    def get_price(kind, lower, upper):
        rows = [r for r in data[kind] if lower <= r["t"] <= upper]
        raw = ([[r["t"], 1, r["c"], r["h"], r["l"], r["o"], 1, "true"] for r in rows]
               if kind == "spot" else rows)
        return {"status": 200, "raw_response_utf8": json.dumps(raw)}
    with patch.object(a, "archive_get", side_effect=get_archive) as files:
        with patch.object(s, "public_get", side_effect=get_price) as prices:
            value = a.capture(tmp_path)
    assert value == data and files.call_count == 6 and prices.call_count == 9
    assert len(list((tmp_path / "source").glob("*.json"))) == 15


def test_archive_unavailable_preserves_binary_and_never_retries(tmp_path):
    prior, new = tmp_path / "v1", tmp_path / "v2"
    failed_parent(prior)
    a.register(new, prior)
    with patch.object(a, "archive_get", return_value=({"status": 404}, b"not found")) as get:
        result = a.run(new)
    assert get.call_count == 1 and result["phases"][-1] == "SOURCE_UNAVAILABLE"
    assert (new / "source/funding-202404.csv.gz").read_bytes() == b"not found"
    with pytest.raises(ValueError):
        a.run(new)


def test_archive_success_never_promotes_or_changes_v1(tmp_path):
    prior, new = tmp_path / "v1", tmp_path / "v2"
    failed_parent(prior)
    a.register(new, prior)
    before = s._path(prior).read_bytes()
    with patch.object(a, "capture", return_value=fixture()):
        result = a.run(new)
    assert result["phases"][-1] == "RESULT" and result["result"]["economic_screen_passed"]
    assert result["profitability_proven"] is False and result["eligible_for_s5"] is False
    assert s._path(prior).read_bytes() == before
    with patch.object(a, "fingerprint", return_value="changed"):
        with pytest.raises(ValueError):
            a.review(new)

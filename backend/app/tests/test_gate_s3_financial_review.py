"""Independent financial oracles and failure-closed offline reader checks."""
import hashlib
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest

from app.strategy import gate_s3_financial_review as review
from app.strategy.gate_s2_replay import Friction
from app.strategy.gate_s3_durable_research import DurableS3ForwardResearch, S3JournalBlocked
from app.tests.test_gate_s3_durable_research import SELECTED, T0, send


def account(report, arm="STANDALONE", symbol="BTCUSDT"):
    return next(a for a in report["accounts"] if a["strategy"] == SELECTED
                and a["arm"] == arm and a["symbol"] == symbol)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_two_closed_trades_and_repeated_snapshot_not_double_counted(tmp_path, side):
    path = tmp_path/"journal.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        send(journal, 0, action=side, filtered="WAIT")
        send(journal, 1)
        send(journal, 2)
        send(journal, 3, high=105 if side == "LONG" else 101,
             low=99 if side == "LONG" else 95)
        send(journal, 4, action=side, filtered="WAIT")
        send(journal, 5)
        send(journal, 6)
        send(journal, 7, high=101 if side == "LONG" else 103,
             low=97 if side == "LONG" else 99)
        send(journal, 8)
        send(journal, 9)
    report = review.financial_review(path)
    a = account(report)
    # First trade +5, second risks .25% of updated 1005 equity => -2.5125.
    assert a["completed_trades"] == 2
    assert a["closed_net_wins"] == 1
    assert a["realized_net_pnl"] == pytest.approx(2.4875)
    assert a["expectancy_net_per_closed_trade"] == pytest.approx(1.24375)
    assert a["profit_factor_net"] == pytest.approx(5/2.5125)
    assert a["exit_counts"] == {"STOP": 1, "TARGET": 1}
    assert account(report, "S1_CONFLUENCE")["completed_trades"] == 0
    assert report["closed_pnl_comparisons"][0]["sample_status"] == "INSUFFICIENT"
    assert not report["profitability_proven"] and not report["execution_authorized"]


def test_costs_match_independent_oracle_without_double_charging(tmp_path):
    path = tmp_path/"costs.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0, action="LONG")
        send(journal, 1)
        send(journal, 2)
        send(journal, 3, high=105)
    a = account(review.financial_review(path))
    entry = 100*1.0006
    exit_price = (entry+4)*.9994
    gross = (exit_price-entry)*1.25
    fees = (entry+exit_price)*1.25*.0006
    funding = entry*1.25*.0001
    assert a["closed_gross_after_slippage_before_fees"] == pytest.approx(gross)
    assert a["closed_fees"] == pytest.approx(fees)
    assert a["closed_funding"] == pytest.approx(funding)
    assert a["realized_net_pnl"] == pytest.approx(gross-fees-funding)
    assert a["profit_factor_net"] is None
    assert a["profit_factor_status"] == "NO_CLOSED_LOSSES"
    assert a["open_net_exit_estimate"] == 0


def test_open_position_separate_and_wal_reader_preserves_journal(tmp_path):
    path = tmp_path/"open.db"
    with DurableS3ForwardResearch(path) as journal:
        for i in range(3):
            send(journal, i, action="LONG" if i == 0 else "WAIT")
        a = account(review.financial_review(path))
        assert a["open_position"] and a["open_net_exit_estimate"] < 0
        assert a["completed_trades"] == 0 and a["closed_fees"] == 0
        assert a["expectancy_net_per_closed_trade"] is None
        send(journal, 3)
        assert review.financial_review(path)["observations"] == 4
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = review.financial_review(path)
    assert review.financial_review(path) == report
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_does_not_create_file_and_empty_is_distinct(tmp_path):
    path = tmp_path/"missing.db"
    with pytest.raises(S3JournalBlocked):
        review.financial_review(path)
    assert not path.exists()
    with DurableS3ForwardResearch(path):
        pass
    report = review.financial_review(path)
    assert report["status"] == "NO_OBSERVATIONS" and not report["accounts"]


def test_changed_prefix_between_reads_is_blocked(tmp_path, monkeypatch):
    path = tmp_path/"race.db"
    original = review.read_only_equity_report
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0)
        def changing(source):
            result = original(source)
            send(journal, 1)
            return result
        monkeypatch.setattr(review, "read_only_equity_report", changing)
        with pytest.raises(S3JournalBlocked, match="advanced or changed"):
            review.financial_review(path)


def test_corruption_blocks_report(tmp_path):
    path = tmp_path/"bad.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE s3_heads SET event_hash='bad'")
    with pytest.raises(S3JournalBlocked):
        review.financial_review(path)


def test_freshness_explicit_and_accounts_not_pooled(tmp_path):
    path = tmp_path/"markets.db"
    with DurableS3ForwardResearch(path) as journal:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            send(journal, 0, symbol=symbol)
    report = review.financial_review(path, as_of=T0+timedelta(minutes=1))
    assert len(report["accounts"]) == 12
    assert all(a["freshness"] == "CURRENT" for a in report["accounts"])
    stale = review.financial_review(path, as_of=T0+timedelta(minutes=31))
    assert all(a["freshness"] == "STALE" for a in stale["accounts"])
    assert report["independent_accounts_not_portfolio"]
    for clock in (T0, T0.replace(tzinfo=None)):
        with pytest.raises(S3JournalBlocked):
            review.financial_review(path, as_of=clock)


def test_cli_exports_elsewhere_and_missing_evidence_has_no_fake_pnl(tmp_path):
    source = tmp_path/"source"
    source.mkdir()
    path = source/"journal.db"
    command = [sys.executable, "-m", "scripts.gate_s3_financial_review", str(path)]
    missing = subprocess.run(command+["--output", str(tmp_path/"missing-output")], capture_output=True)
    assert missing.returncode == 2 and b"EVIDENCE_UNAVAILABLE" in missing.stderr
    assert not (tmp_path/"missing-output").exists()
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0)
    before = path.read_bytes()
    bad = subprocess.run(command+["--output", str(source/"output")], capture_output=True)
    assert bad.returncode == 2
    good = subprocess.run(command+["--output", str(tmp_path/"report")], capture_output=True)
    assert good.returncode == 0, good.stderr.decode()
    assert (tmp_path/"report"/"financial-review.json").is_file()
    assert "pesquisa hipotética" in (tmp_path/"report"/"financial-review.md").read_text()
    assert path.read_bytes() == before

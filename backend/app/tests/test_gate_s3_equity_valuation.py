"""Independent equity evidence, verified replay, costs and read-only guarantees."""
import hashlib
import sqlite3

import pytest

from app.strategy.gate_s2_replay import Friction
from app.strategy.gate_s3_durable_research import DurableS3ForwardResearch, S3JournalBlocked
from app.strategy.gate_s3_equity_valuation import read_only_equity_report
from app.tests.test_gate_s3_durable_research import (
    SELECTED, candle, shadow, send,
)


def account(report, arm="STANDALONE", symbol="BTCUSDT"):
    return next(a for a in report["accounts"] if a["strategy"] == SELECTED
                and a["arm"] == arm and a["symbol"] == symbol)


def record_close(journal, index, close, **kwargs):
    bar = candle(index, **kwargs).model_copy(update={"close": close})
    return journal.record(bar, shadow(bar), observed_at=bar.received_at)


@pytest.mark.parametrize("side,close,expected", [
    ("LONG", 99., -1.25), ("SHORT", 101., -1.25),
    ("LONG", 101., 1.25), ("SHORT", 99., 1.25),
])
def test_open_loss_or_gain_visible_before_trade_closes(tmp_path, side, close, expected):
    path = tmp_path/"research.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        send(journal, 0, action=side, filtered="WAIT")
        send(journal, 1)
        record_close(journal, 2, close)
    report = read_only_equity_report(path)
    a = account(report)
    assert a["realized_net_pnl"] == 0 and a["completed_trades"] == 0
    assert a["open_net_exit_estimate"] == expected
    assert a["equity_including_open_exit_estimate"] == 1000+expected
    assert account(report, "S1_CONFLUENCE")["total_net_exit_estimate"] == 0
    assert not report["execution_authorized"]
    assert not report["risk_policy_modified"]


def test_peak_to_trough_measures_open_equity_and_survives_new_reader(tmp_path):
    path = tmp_path/"research.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        send(journal, 0, action="LONG")
        send(journal, 1)
        record_close(journal, 2, 101.)
        record_close(journal, 3, 99.)
    report = read_only_equity_report(path)
    a = account(report)
    assert a["realized_only_max_drawdown_pct"] == 0
    assert a["candle_close_peak_equity"] == 1001.25
    assert a["candle_close_max_drawdown_pct"] == pytest.approx(
        (1-998.75/1001.25)*100, abs=1e-6)
    assert read_only_equity_report(path) == report


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_costs_include_entry_and_exit_fee_and_adverse_slippage(tmp_path, side):
    path = tmp_path/"costs.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0, action=side)
        send(journal, 1)
        send(journal, 2)
    a = account(read_only_equity_report(path))
    entry = 100*(1.0006 if side == "LONG" else .9994)
    exit_price = 100*(.9994 if side == "LONG" else 1.0006)
    quantity = 1.25
    expected_gross = (exit_price-entry)*quantity*(1 if side == "LONG" else -1)
    expected_fees = (entry+exit_price)*quantity*.0006
    expected_funding = entry*quantity*.0001
    assert a["open_gross_after_slippage"] == pytest.approx(expected_gross)
    assert a["open_estimated_roundtrip_fees"] == pytest.approx(expected_fees)
    assert a["open_estimated_funding"] == pytest.approx(expected_funding)
    assert a["total_net_exit_estimate"] == pytest.approx(
        expected_gross-expected_fees-expected_funding)


def test_closed_position_costs_are_not_charged_twice(tmp_path):
    path = tmp_path/"closed.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0, action="LONG")
        send(journal, 1)
        send(journal, 2)
        closed = send(journal, 3, high=105., low=99.)
    a = account(read_only_equity_report(path))
    original = next(a for a in closed["accounts"]
                    if a["strategy"] == SELECTED and a["arm"] == "STANDALONE")
    assert a["realized_net_pnl"] == original["net_pnl"]
    assert a["total_net_exit_estimate"] == original["net_pnl"]
    assert not a["open_position"]
    assert a["open_net_exit_estimate"] == 0
    assert a["open_estimated_roundtrip_fees"] == 0


def test_read_only_replay_does_not_change_source_or_legacy_outcomes(tmp_path):
    path = tmp_path/"unchanged.db"
    with DurableS3ForwardResearch(path) as journal:
        for i in range(4):
            send(journal, i, action="LONG" if i == 0 else "WAIT")
        before_snapshot = journal.scorer.snapshot("BTCUSDT")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = read_only_equity_report(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert report["observations"] == 4
    assert report["source_database_modified"] is False
    with DurableS3ForwardResearch(path) as recovered:
        assert recovered.scorer.snapshot("BTCUSDT") == before_snapshot


def test_report_reads_consistent_committed_wal_without_stopping_writer(tmp_path):
    path = tmp_path/"active.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0, action="LONG")
        assert read_only_equity_report(path)["observations"] == 1
        send(journal, 1)
        assert read_only_equity_report(path)["observations"] == 2
        send(journal, 2)
        assert account(read_only_equity_report(path))["open_position"]


@pytest.mark.parametrize("tamper", ["payload", "outcome", "head", "model", "schema"])
def test_corrupted_or_incompatible_evidence_is_rejected(tmp_path, tamper):
    path = tmp_path/"corrupted.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0, action="LONG")
    with sqlite3.connect(path) as db:
        if tamper in ("payload", "outcome"):
            db.execute("DROP TRIGGER s3_no_event_update")
            column = "payload_json" if tamper == "payload" else "outcome_json"
            db.execute(f"UPDATE s3_observations SET {column}='{{}}'")
        elif tamper == "head":
            db.execute("UPDATE s3_heads SET event_count=999")
        else:
            name = "model_parameters" if tamper == "model" else "forward_research"
            db.execute("UPDATE s3_schema SET version='{}' WHERE name=?", (name,))
    with pytest.raises(S3JournalBlocked):
        read_only_equity_report(path)


def test_missing_source_is_never_created_and_empty_journal_has_no_profit(tmp_path):
    path = tmp_path/"missing.db"
    with pytest.raises(S3JournalBlocked):
        read_only_equity_report(path)
    assert not path.exists()
    with DurableS3ForwardResearch(path):
        pass
    report = read_only_equity_report(path)
    assert report["status"] == "NO_OBSERVATIONS" and report["accounts"] == []
    assert not report["profitability_proven"]


def test_independent_market_books_are_not_added_as_a_portfolio(tmp_path):
    path = tmp_path/"markets.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        for i in range(3):
            for symbol in ("BTCUSDT", "ETHUSDT"):
                send(journal, i, symbol=symbol,
                     action="LONG" if i == 0 and symbol == "BTCUSDT" else "WAIT")
    report = read_only_equity_report(path)
    assert account(report, symbol="BTCUSDT")["open_position"]
    assert not account(report, symbol="ETHUSDT")["open_position"]
    assert report["independent_accounts_not_portfolio"]
    assert "portfolio_pnl" not in report

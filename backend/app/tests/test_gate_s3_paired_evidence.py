"""Compare verified research records without turning descriptive gains into approval."""
from datetime import timedelta
import hashlib
import json
import sqlite3

import pytest

from app.strategy.gate_s2_replay import Friction, ResearchRisk
from app.strategy.gate_s2d_forward import FROZEN_AT
from app.strategy.gate_s3_durable_research import DurableS3ForwardResearch, S3JournalBlocked
from app.strategy.gate_s3_paired_evidence import read_only_comparison_report, main
from app.tests.test_gate_s3_durable_research import SELECTED, candle, send, shadow
from app.tests.test_gate_s3_equity_valuation import record_close


def pair(report, symbol="BTCUSDT"):
    return next(p for p in report["comparisons"]
                if p["symbol"] == symbol and p["strategy"] == SELECTED)


def test_losing_less_is_explicitly_still_worse_than_cash(tmp_path):
    path = tmp_path/"losses.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        for i in range(6):
            send(journal, i, action="LONG" if i % 3 == 0 else "WAIT",
                 filtered="LONG" if i == 0 else "WAIT",
                 low=97. if i % 3 == 2 else 99.)
    p = pair(read_only_comparison_report(path))
    assert p["baseline"]["realized_net_pnl"] < p["confluence"]["realized_net_pnl"] < 0
    assert p["estimated_delta_vs_baseline"] > 0
    assert p["estimated_delta_vs_cash"] < 0
    assert p["descriptive_outcome"] == "REDUCED_LOSS_STILL_BELOW_CASH"
    assert p["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert not p["economic_edge_proven"]


def test_open_loss_is_included_in_the_paired_comparison(tmp_path):
    path = tmp_path/"open.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        send(journal, 0, action="LONG", filtered="WAIT")
        send(journal, 1)
        record_close(journal, 2, 99.)
    p = pair(read_only_comparison_report(path))
    assert p["baseline"]["realized_net_pnl"] == 0
    assert p["estimated_delta_vs_baseline"] == 1.25
    assert p["descriptive_outcome"] == "MATCHED_CASH"
    assert p["confluence"]["completed_trades"] == 0


def test_one_profitable_trade_never_meets_the_sample_floor(tmp_path):
    path = tmp_path/"one.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        send(journal, 0, action="LONG")
        send(journal, 1)
        send(journal, 2, high=105.)
    report = read_only_comparison_report(path)
    p = pair(report)
    assert p["estimated_delta_vs_cash"] == 5.
    assert p["descriptive_outcome"] == "POSITIVE_WITHOUT_BASELINE_IMPROVEMENT"
    assert len(p["insufficiency_reasons"]) == 2
    assert not report["profitability_proven"]
    assert not p["automatic_promotion_authorized"]


def test_beating_both_benchmarks_still_does_not_prove_edge(tmp_path):
    path = tmp_path/"positive.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        for i in range(6):
            send(journal, i, action="LONG" if i % 3 == 0 else "WAIT",
                 filtered="LONG" if i == 3 else "WAIT",
                 low=97. if i == 2 else 99., high=105. if i == 5 else 101.)
    p = pair(read_only_comparison_report(path))
    assert p["estimated_delta_vs_baseline"] > 0 and p["estimated_delta_vs_cash"] > 0
    assert p["descriptive_outcome"] == "ABOVE_BASELINE_AND_CASH_IN_SAMPLE"
    assert p["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert not p["economic_edge_proven"] and not p["order_submission_authorized"]


@pytest.mark.parametrize("bars,enough_bars", [(95, False), (96, True)])
def test_thirty_closed_trades_are_preliminary_only_and_need_96_bars(tmp_path, bars, enough_bars):
    path = tmp_path/"floor.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        for i in range(bars):
            send(journal, i, action="LONG" if i < 90 and i % 3 == 0 else "WAIT",
                 high=105. if i < 90 and i % 3 == 2 else 101.)
    report = read_only_comparison_report(path)
    p = pair(report)
    assert p["baseline"]["completed_trades"] == p["confluence"]["completed_trades"] == 30
    assert p["closed_trade_sample_floor_met"]
    assert p["post_freeze_candle_floor_met"] is enough_bars
    assert p["window"]["post_freeze_candles"] == bars
    assert (p["evidence_status"] == "PRELIMINARY_DESCRIPTIVE_SAMPLE") is enough_bars
    assert not p["economic_edge_proven"] and not report["statistical_significance_established"]
    assert not report["profitability_proven"] and not report["execution_authorized"]


def test_no_trades_does_not_become_a_valid_trader_after_96_candles(tmp_path):
    path = tmp_path/"cash.db"
    with DurableS3ForwardResearch(path) as journal:
        for i in range(96):
            send(journal, i)
    p = pair(read_only_comparison_report(path))
    assert p["descriptive_outcome"] == "MATCHED_CASH"
    assert p["insufficiency_reasons"] == ["INSUFFICIENT_CLOSED_TRADES_PER_ARM"]


def test_filtered_inactivity_cannot_borrow_sample_from_baseline(tmp_path):
    path = tmp_path/"filtered-cash.db"
    with DurableS3ForwardResearch(path, friction=Friction(0, 0, 0)) as journal:
        for i in range(96):
            send(journal, i, action="LONG" if i < 90 and i % 3 == 0 else "WAIT",
                 filtered="WAIT", high=105. if i < 90 and i % 3 == 2 else 101.)
    p = pair(read_only_comparison_report(path))
    assert p["baseline"]["completed_trades"] == 30
    assert p["confluence"]["completed_trades"] == 0
    assert not p["closed_trade_sample_floor_met"]


@pytest.mark.parametrize("configured,expected", [(1, 30), (50, 50)])
def test_journal_config_cannot_lower_the_protocol_floor(tmp_path, configured, expected):
    path = tmp_path/"config.db"
    with DurableS3ForwardResearch(path, risk=ResearchRisk(min_oos_evidence_trades=configured)):
        pass
    assert read_only_comparison_report(path)["minimum_closed_trades_each_arm"] == expected


def test_warmup_and_freeze_boundary_do_not_count_as_new_evidence(tmp_path):
    path = tmp_path/"boundary.db"
    with DurableS3ForwardResearch(path) as journal:
        for offset in (-15, 0, 15):
            ts = FROZEN_AT+timedelta(minutes=offset)
            bar = candle(0).model_copy(update={
                "closed_at": ts, "received_at": ts+timedelta(seconds=5)})
            journal.record(bar, shadow(bar), observed_at=bar.received_at)
    report = read_only_comparison_report(path)
    assert report["source_observations"] == 3
    assert pair(report)["window"]["post_freeze_candles"] == 1


def test_markets_cannot_pool_bars_to_meet_a_sample_floor(tmp_path):
    path = tmp_path/"markets.db"
    with DurableS3ForwardResearch(path) as journal:
        for i in range(48):
            for symbol in ("BTCUSDT", "ETHUSDT"):
                send(journal, i, symbol=symbol)
    report = read_only_comparison_report(path)
    assert report["source_observations"] == 96
    for symbol in ("BTCUSDT", "ETHUSDT"):
        p = pair(report, symbol)
        assert p["window"]["post_freeze_candles"] == 48
        assert not p["post_freeze_candle_floor_met"]
    assert not report["portfolio_performance_proven"]


def test_verified_source_is_unchanged_and_corrupt_source_cannot_be_compared(tmp_path):
    path = tmp_path/"source.db"
    with DurableS3ForwardResearch(path) as journal:
        send(journal, 0)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = read_only_comparison_report(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert report == read_only_comparison_report(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE s3_heads SET event_count=99")
    with pytest.raises(S3JournalBlocked):
        read_only_comparison_report(path)


def test_empty_journal_and_cli_never_invent_a_result(tmp_path, capsys, monkeypatch):
    path = tmp_path/"empty.db"
    with DurableS3ForwardResearch(path):
        pass
    monkeypatch.setattr("sys.argv", ["s3-evidence", str(path)])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "NO_ELIGIBLE_ACCOUNTS"
    assert report["comparisons"] == []
    assert not report["execution_authorized"]
    monkeypatch.setattr("sys.argv", ["s3-evidence", str(tmp_path/"missing.db")])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not (tmp_path/"missing.db").exists()


def test_cli_reports_storage_open_failure_without_a_result(tmp_path, capsys, monkeypatch):
    path = tmp_path/"unavailable.db"
    with DurableS3ForwardResearch(path):
        pass
    def failed_open(*args, **kwargs):
        raise sqlite3.OperationalError("simulated read failure")
    monkeypatch.setattr("app.strategy.gate_s3_equity_valuation.sqlite3.connect", failed_open)
    monkeypatch.setattr("sys.argv", ["s3-evidence", str(path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not capsys.readouterr().out

"""Independent arithmetic oracles, dependent-series diagnostics and suite boundaries."""
import json
import math
import random
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace

import pytest

from app.tests.test_gate_s4_s5_lab import candles
from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_statistics as stats
from app.strategy import gate_s4_validation as validation
from app.strategy import gate_s4_public_snapshot as capture
from app.strategy.gate_s2_replay import Friction, Trade


def test_holm_known_oracle_and_permutations_do_not_drop_failed_trials():
    p = [.01, .04, .03, .5]
    assert stats.holm_adjust(p) == pytest.approx([.04, .09, .09, .5])
    assert stats.holm_adjust([.001, 1, 1, 1])[0] == .004
    assert stats.holm_adjust([.03, .01, .5, .04]) == pytest.approx([.09, .04, .5, .09])
    assert stats.holm_adjust([.02, .02]) == [.04, .04]
    for invalid in ([], [float("nan")], [-.1], [1.1], [True]):
        with pytest.raises(ValueError):
            stats.holm_adjust(invalid)


def reference_chunk_draws(folds, length, replications, seed):
    rng = random.Random(seed)
    output = []
    for _ in range(replications):
        values = []
        for fold in folds:
            remaining = len(fold)
            while remaining:
                start = rng.randrange(len(fold))
                chunk = (1 if length == 1 else
                         1 + int(math.log1p(-rng.random()) / math.log1p(-1 / length)))
                chunk = min(chunk, remaining)
                values.extend(fold[(start + i) % len(fold)] for i in range(chunk))
                remaining -= chunk
        output.append(fmean(values))
    return output


@pytest.mark.parametrize("length", [1, 8, 16, 32])
def test_stationary_prefix_sum_matches_independent_expanded_index_oracle(length):
    folds = [tuple((i % 11 - 5) / 1000 for i in range(80)),
             tuple((i % 7 - 3) / 500 for i in range(96))]
    actual = stats.bootstrap_means(folds, block_length=length, replications=99, seed=19)
    expected = reference_chunk_draws(folds, length, 99, 19)
    assert actual == pytest.approx(expected, abs=1e-14)
    assert actual == stats.bootstrap_means(folds, block_length=length, replications=99, seed=19)


def test_positive_synthetic_fixture_and_null_do_not_create_authorization():
    strong = tuple(.01 + .001 * (-1 if i % 2 else 1) for i in range(320))
    r = stats.mean_diagnostic([strong, strong], replications=199)
    assert r["marginal_p_value"] == .005
    assert r["lower_pointwise_95_mean"] > 0
    assert not r["stationarity_and_mixing_verified"]
    assert not r["exact_finite_sample_test"]
    assert [v["expected_block_length"] for v in r["sensitivity"]] == [8, 16, 32]
    assert stats.mean_diagnostic([(-.01,) * 320] * 2)["marginal_p_value"] == 1
    assert stats.mean_diagnostic([(-.01, .01) * 160] * 2)["marginal_p_value"] == 1
    assert stats.mean_diagnostic([(.01,) * 320] * 2)["status"] == "DEGENERATE_TIME_SERIES"
    assert stats.mean_diagnostic([strong[:100]] * 2)["status"] == "INSUFFICIENT_TIME_BLOCKS"


def test_open_loss_is_in_economic_series_and_boundary_mark_reconciles():
    bars = candles(100)
    for i, price in ((81, 101), (82, 85), (83, 95), (84, 102)):
        bars[i] = bars[i].model_copy(update={"close": price})
    trade = Trade("X", "LONG", 80, 81, 84, 100, 102, 1, 80, 120, 2, 0, 0, 2,
                  "END_WINDOW_ESTIMATE")
    report = SimpleNamespace(trades=[trade], start_equity=1000, net_pnl=2,
                             estimated_end_window_exits=1)
    result = validation.equity_changes(report, bars, start=80, end=85, friction=Friction(0, 0, 0))
    assert result["changes_over_initial_equity"] == pytest.approx([0, .001, -.016, .010, .007])
    assert result["candle_close_drawdown_pct"] == pytest.approx(16 / 1001 * 100)
    assert sum(result["changes_over_initial_equity"]) == pytest.approx(.002)
    assert lab.trade_metrics(report)["completed_trades"] == 0


@pytest.mark.parametrize("direction", [1, -1])
def test_net_equity_mark_independent_fee_slippage_and_funding_oracle(direction):
    bars = candles(100)
    bars[81] = bars[81].model_copy(update={"close": 104})
    bars[82] = bars[82].model_copy(update={"close": 106})
    entry, quantity = 100., 2.
    price = 106 * (1 - direction * .0006)
    fees = (entry + price) * quantity * .0006
    funding = entry * quantity * .0001
    net = (price - entry) * quantity * direction - fees - funding
    trade = Trade("X", "LONG" if direction == 1 else "SHORT", 80, 81, 82,
                  entry, price, quantity, 90, 110, net + fees + funding, fees, funding,
                  net, "END_WINDOW_ESTIMATE")
    source = SimpleNamespace(trades=[trade], start_equity=1000, net_pnl=net)
    result = validation.equity_changes(source, bars, start=80, end=83, friction=Friction())
    assert sum(result["changes_over_initial_equity"]) * 1000 == pytest.approx(net)
    mark81 = 104 * (1 - direction * .0006)
    net81 = (mark81 - 100) * 2 * direction - (100 + mark81) * 2 * .0006 - .02
    assert result["changes_over_initial_equity"][1] * 1000 == pytest.approx(net81)


def catalog_fixture(tmp_path, *, available=False):
    series = [{"symbol": symbol, "timeframe": tf, "status": "UNAVAILABLE", "reason": "FIXTURE"}
              for symbol, tf in sorted(validation.SERIES)]
    if available:
        folder = tmp_path / "source"
        folder.mkdir()
        dataset = folder / "candles.json"
        dataset.write_text(json.dumps([b.model_dump(mode="json") for b in candles()]))
        (folder / "source.json").write_text('{"fixture":true}')
        _, sha = lab.load_dataset(dataset)
        item = next(v for v in series if (v["symbol"], v["timeframe"]) == ("BTCUSDT", "15m"))
        item.update(status="AVAILABLE", path=str(dataset), sha256=sha, candles=600)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"venue": "GATE_FUTURES_TESTNET_PUBLIC", "requested_series": 9,
                                   "series": series}))
    return catalog


def test_whole_family_keeps_unavailable_sources_and_cannot_retry(tmp_path, capsys):
    root = tmp_path / "suite"
    manifest = validation.register_suite(root, catalog_fixture(tmp_path))
    assert len(manifest["trials"]) == 54 and manifest["family_tests"] == 324
    validation.run_suite(root)
    before = (root / "s4-validation.sqlite3").read_bytes()
    report = validation.suite_report(root)
    assert report["recorded_trials"] == report["failed_trials_including_unavailable_sources"] == 54
    assert report["complete"] and not report["source_coverage_complete"]
    assert report["family_tests_including_failed"] == 324
    assert report["historical_screen_survivors"] == 0
    assert all(t["holm_adjusted_p_value"] == 1 for t in report["statistical_tests"])
    for key in ("profitability_proven", "execution_authorized", "automatic_promotion_authorized",
                "holdout_evaluated", "prospective_evidence", "statistical_significance_established"):
        assert report[key] is False
    assert before == (root / "s4-validation.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="already started"):
        validation.run_suite(root)
    with pytest.raises(FileExistsError):
        validation.register_suite(root, tmp_path / "catalog.json")


def test_source_or_engine_mutation_blocks_before_start(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    catalog = catalog_fixture(tmp_path, available=True)
    validation.register_suite(root, catalog)
    source = tmp_path / "source" / "source.json"
    source.write_text(source.read_text() + " ")
    with pytest.raises(ValueError, match="source evidence changed"):
        validation.run_suite(root)
    monkeypatch.setattr(validation, "engine_fingerprint", lambda: "changed")
    with pytest.raises(ValueError, match="implementation/policy"):
        validation.suite_report(root)


def test_journal_corruption_cannot_silently_become_a_missing_trial(tmp_path):
    root = tmp_path / "suite"
    validation.register_suite(root, catalog_fixture(tmp_path))
    with sqlite3.connect(root / "s4-validation.sqlite3") as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("UPDATE events SET payload='{}'")
    with pytest.raises(ValueError, match="integrity"):
        validation.suite_report(root)


def test_memoized_plans_equal_original_rules_and_never_accept_holdout():
    bars = tuple(candles(600))
    cache = validation.PrefixPlans(bars)
    for h in lab.seed_hypotheses():
        for delayed in (False, True):
            prefix = bars[:280]
            clock = prefix[-1].closed_at + timedelta(microseconds=1)
            actual = cache.plan(h.parent_strategy, prefix, clock, hypothesis=h, delayed=delayed)
            expected = lab.evaluate_hypothesis(h, prefix, clock, delayed=delayed)
            assert actual == expected
    with pytest.raises(ValueError, match="causal development"):
        cache.plan(lab.seed_hypotheses()[0].parent_strategy, bars[:500], bars[-1].received_at)


def test_real_trial_reconciles_all_scenarios_without_evaluating_holdout():
    bars = tuple(candles(600))
    report = validation.evaluate_trial(lab.seed_hypotheses()[0], bars, validation.PrefixPlans(bars))
    assert not report["holdout_evaluated"]
    for fold in report["folds"]:
        for scenario in validation.SCENARIOS:
            for name in (scenario, scenario + "_parent"):
                value = fold[name]
                assert sum(value["changes_over_initial_equity"]) * 1000 == pytest.approx(
                    value["net_including_boundary_mark"], abs=1e-7)
    changed = bars[:480] + tuple(b.model_copy(update={"close": 99999}) for b in bars[480:])
    second = validation.evaluate_trial(lab.seed_hypotheses()[0], changed, validation.PrefixPlans(changed))
    assert report == second


def test_public_capture_archives_raw_invalid_page_and_does_not_invent_candle(tmp_path, monkeypatch):
    calls = []
    def response(path, params=None):
        calls.append((path, params))
        stamp = candles()[0].received_at
        if "contracts/" in path:
            return {"name": "ETH_USDT", "quanto_multiplier": "1"}, {"fixture": "metadata"}, stamp
        return [{"t": 123, "o": "0"}], {"fixture": "invalid_raw_page"}, stamp
    monkeypatch.setattr(capture, "_get", response)
    result = capture.capture_series(tmp_path, "ETHUSDT", "4h", count=600)
    assert result["status"] == "UNAVAILABLE"
    evidence = json.loads((tmp_path / "ETHUSDT-4h" / "source.json").read_text())
    assert len(evidence["raw_pages"]) == 2
    assert not (tmp_path / "ETHUSDT-4h" / "candles.json").exists()
    assert calls[1][0] == "/futures/usdt/candlesticks"

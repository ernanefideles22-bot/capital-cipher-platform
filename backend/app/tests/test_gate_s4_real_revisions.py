"""Original-parent parity, causal train/test separation and public-read boundaries."""
import json
import random
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.schemas.market import Candle
from app.strategy import gate_s4_lab as lab
from app.strategy import gate_s4_real_source as source
from app.strategy import gate_s4_real_study as study
from app.strategy.gate_classical_s2 import ResearchPlan, STRATEGIES, evaluate_strategy
from app.strategy.gate_s2_replay import Friction, replay_segment
from app.strategy.gate_s4_revision_engine import CausalParentPlans, Revision, revision_specs


def series(n=900, *, tf="15m"):
    rng = random.Random(20261103)
    price, bars = 100., []
    step = {"15m": 900, "1h": 3600, "4h": 14400}[tf]
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        close = max(20., price + rng.gauss(0, .7))
        stamp = start + timedelta(seconds=(i + 1) * step)
        bars.append(Candle(exchange="GATE", symbol="BTCUSDT", timeframe=tf,
            open=price, high=max(price, close) + .1, low=min(price, close) - .1,
            close=close, volume=rng.uniform(10, 500), closed_at=stamp,
            received_at=stamp + timedelta(seconds=3)))
        price = close
    return tuple(bars)


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
def test_incremental_parent_matches_unchanged_s2_at_every_sampled_prefix(tf):
    bars = series(tf=tf)
    plans = CausalParentPlans(bars, cap=800)
    for count in range(80, 801, 13):
        clock = bars[count - 1].closed_at + timedelta(microseconds=1)
        for strategy in STRATEGIES:
            actual = plans.parent(strategy, count, clock)
            expected = evaluate_strategy(strategy, bars[:count], as_of=clock, historical_replay=True)
            assert actual == expected
    with pytest.raises(ValueError):
        plans.parent(next(iter(STRATEGIES)), 801, bars[-1].received_at)


@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_full_replay_matches_original_parent_trade_for_trade(strategy):
    bars = series(600)
    cache = CausalParentPlans(bars, cap=500)
    expected = replay_segment(strategy, bars[:500], start_index=220, end_index=500, segment="PARITY")
    actual = replay_segment(strategy, bars[:500], start_index=220, end_index=500, segment="PARITY",
                           plan_evaluator=lambda s, p, t: cache.plan(s, p, t))
    assert actual == expected


def test_future_changes_cannot_change_training_or_validation_parent_features():
    bars = series(900)
    modified = bars[:540] + tuple(b.model_copy(update={"close": 99999, "high": 100000}) for b in bars[540:])
    a, b = CausalParentPlans(bars, cap=540), CausalParentPlans(modified, cap=540)
    for strategy in STRATEGIES:
        clock = bars[539].closed_at + timedelta(microseconds=1)
        assert a.parent(strategy, 540, clock) == b.parent(strategy, 540, clock)
    with pytest.raises(ValueError):
        a.plan(next(iter(STRATEGIES)), bars[:541], bars[541].received_at)


def test_legacy_filters_equal_prior_lab_and_delayed_cutoff_is_current():
    bars = series(900)
    cache = CausalParentPlans(bars, cap=720)
    for h in lab.seed_hypotheses():
        for delayed in (False, True):
            prefix = bars[:600]
            clock = prefix[-1].closed_at + timedelta(microseconds=1)
            actual = cache.plan(h.parent_strategy, prefix, clock, hypothesis=h, delayed=delayed)
            expected = lab.evaluate_hypothesis(h, prefix, clock, delayed=delayed)
            # Rule decisions/risk must match; only abstention explanation differs.
            assert {k: v for k, v in asdict(actual).items() if k != "reasoning"} == {
                k: v for k, v in asdict(expected).items() if k != "reasoning"}


@pytest.mark.parametrize("strategy,rule", [("X", "EFFICIENCY20_AT_MOST_0_25"),
    ("EMA_TREND_PULLBACK_V1", "exec('order')"), ("EMA_TREND_PULLBACK_V1", "EMA200_DIRECTION_AND_SLOPE")])
def test_revision_interface_rejects_unregistered_rules(strategy, rule):
    with pytest.raises(ValueError):
        Revision(strategy, rule, "Explicit research hypothesis with no arbitrary executable code.")


def test_revision_budget_and_thresholds_are_fixed_and_never_change_risk():
    specs = revision_specs()
    assert len(specs) == 3 and len({h.candidate_id for h in specs}) == 3
    bars = series()
    cache = CausalParentPlans(bars, cap=720)
    for count in range(220, 720, 17):
        clock = bars[count - 1].closed_at + timedelta(microseconds=1)
        for h in specs:
            parent = cache.parent(h.parent_strategy, count, clock)
            revised = cache.plan(h.parent_strategy, bars[:count], clock, hypothesis=h)
            assert not revised.execution_authorized and not revised.predictive_edge_proven
            assert revised.action in ("WAIT", parent.action)
            if revised.action != "WAIT":
                assert revised.stop_distance == parent.stop_distance
                assert revised.target_distance == parent.target_distance


def test_cost_floor_has_an_independent_basis_point_oracle(monkeypatch):
    bars = series()
    cache = CausalParentPlans(bars, cap=720)
    h = revision_specs()[0]
    count = 500
    clock = bars[count - 1].closed_at + timedelta(microseconds=1)
    price = bars[count - 1].close
    # Doubled costs: 12 fee + 12 slip bps each side + 2 funding bps = 50 bps.
    # Three budgets = 150 bps = 1.5 percent of the decision close.
    minimum_target = price * .015
    parent = ResearchPlan(h.parent_strategy, "1.0.0", "BTCUSDT", "15m", clock,
                          "LONG", 1., minimum_target - .0001, "fixture")
    monkeypatch.setattr(cache, "parent", lambda *a: parent)
    assert cache.plan(h.parent_strategy, bars[:count], clock, hypothesis=h).action == "WAIT"
    parent = replace(parent, target_distance=minimum_target + .0001)
    actual = cache.plan(h.parent_strategy, bars[:count], clock, hypothesis=h)
    assert actual.action == "LONG" and actual.stop_distance == 1.
    assert not actual.execution_authorized


def test_temporal_windows_keep_diagnosis_before_validation_and_original_tail_reserved():
    w = study.windows(6000)
    assert w["diagnosis"] == [220, 3600]
    assert w["validation"] == [[3625, 4200], [4225, 4800]]
    assert w["reserved_holdout"] == [4800, 6000]
    assert w["validation"][0][0] - w["diagnosis"][1] > 24
    assert w["validation"][1][0] - w["validation"][0][1] > 24
    assert w["validation"][-1][1] <= 5400  # Original 3000-bar Testnet final 600 preserved.


def test_source_comparison_matches_timezone_instants_and_excludes_later_prices():
    bars = series(900)
    values = [b.model_dump(mode="json") for b in bars]
    assert values[0]["closed_at"].endswith("Z")
    for v in values[540:]:
        v["close"] *= 2
    result = study.compare_training_sources(bars, values, 540)
    assert result["matched_candles"] == result["equal_close_candles"] == 540
    assert result["max_abs_close_difference_pct"] == 0


def unavailable_catalog(tmp_path):
    rows = [{"symbol": s, "timeframe": tf, "status": "UNAVAILABLE", "reason": "FIXTURE"}
            for s, tf in sorted(study.SERIES)]
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"venue": source.VENUE, "requested_series": 9, "series": rows}))
    return p, rows


def test_all_81_trials_and_486_tests_survive_source_failure_and_read_is_only_read(tmp_path, capsys):
    catalog, rows = unavailable_catalog(tmp_path)
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "financial-statistical-review.json").write_text('{"fixture":true}')
    diagnosis = tmp_path / "diagnosis.json"
    study.diagnose(catalog, prior, diagnosis)
    root = tmp_path / "study"
    m = study.register(root, catalog, diagnosis)
    assert len(m["trials"]) == 81 and m["family_tests"] == 486
    assert sum(h["kind"] == "REVISION" for h in m["hypotheses"]) == 3
    study.run(root)
    path = root / "s4-real-study.sqlite3"
    original = path.read_bytes()
    r = study.report(root)
    assert r["complete"] and r["recorded_trials"] == r["failed_trials"] == 81
    assert r["family_tests"] == 486 and r["historical_survivors"] == 0
    assert path.read_bytes() == original
    assert all(t["holm_adjusted_p_value"] == 1 for t in r["statistical_tests"])
    for key in ("holdout_evaluated", "profitability_proven", "execution_authorized", "automatic_promotion_authorized"):
        assert r[key] is False
    with pytest.raises(ValueError, match="already started"):
        study.run(root)
    with pytest.raises(FileExistsError):
        study.register(root, catalog, diagnosis)


def test_training_diagnosis_fingerprint_cannot_be_reused_after_engine_change(tmp_path, monkeypatch):
    catalog, _ = unavailable_catalog(tmp_path)
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "financial-statistical-review.json").write_text('{}')
    diagnosis = tmp_path / "diagnosis.json"
    study.diagnose(catalog, prior, diagnosis)
    monkeypatch.setattr(study, "fingerprint", lambda: "changed")
    with pytest.raises(ValueError, match="diagnosis"):
        study.register(tmp_path / "study", catalog, diagnosis)


def test_public_source_whitelist_excludes_every_account_and_order_endpoint():
    for p in ("/futures/usdt/orders", "/futures/usdt/accounts", "/spot/accounts",
              "https://example.com", "/wallet/withdrawals"):
        with pytest.raises(ValueError, match="whitelisted"):
            source.public_get(p)
    assert source.ORIGIN == "https://api.gateio.ws"
    assert not source._NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://evil.invalid")


def test_invalid_public_page_is_archived_and_never_repaired(tmp_path, monkeypatch):
    def public(path, params=None):
        stamp = datetime.now(timezone.utc)
        if "/contracts/" in path:
            return {"name": "ETH_USDT", "quanto_multiplier": "1"}, {"fixture": "metadata"}, stamp
        return [{"t": 123, "o": "0"}], {"fixture": "invalid"}, stamp
    monkeypatch.setattr(source, "public_get", public)
    r = source.capture_real_series(tmp_path, "ETHUSDT", "4h", anchor=1793714117, count=3000)
    assert r["status"] == "UNAVAILABLE"
    saved = json.loads((tmp_path / "ETHUSDT-4h" / "source.json").read_text())
    assert len(saved["raw_pages"]) == 2
    assert not saved["account_access_used"] and not saved["order_submission_authorized"]

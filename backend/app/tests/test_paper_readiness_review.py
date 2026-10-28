"""Offline review must not convert weak observations into execution authority."""

import copy
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/review_paper_readiness.py"
SPEC = importlib.util.spec_from_file_location("readiness_review", SCRIPT)
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)
o = r.observer


def snapshot():
    status = {"mode": "PAPER", "source_revision": o.EXPECTED_REVISION,
              "oms": {"environment": "PAPER", "live_execution_available": False}}
    card = {"agent_name": "ExampleAgent", "agent_version": "1.0.0",
            "sample_count": 147, "directional_sample_count": 6,
            "accuracy": 5 / 6, "mean_brier_loss": 0.24,
            "mean_marginal_contribution": 0.0001}
    return o.seal({
        "schema": o.SCHEMA, "status": "OBSERVING", "origin": o.DEFAULT_ORIGIN,
        "completed_at": "2026-10-28T01:13:00+00:00",
        "comparison": {"comparable": True, "closed_trades": 0, "reported_realized_net_pnl": 0},
        "data": {"status": status, "performance": {"open_trades": 0, "net_pnl": -49.4088,
                  "fees_total": 16, "slippage_total": 16.183},
                 "orders": {"orders": []}, "scorecards": {"scorecards": [card]},
                 "candidates": {"candidates": [{**card, "eligible_for_regime_shadow_test": True}]},
                 "regimes": {"shadow_specialist_count": 0, "unavailable_historical_forecasts": 1030},
                 "timeframes": {"breakdown": []}, "orchestrator": {"cycle_latency_ms": {"p95": 553}}},
    })


def order(identity, side="BUY", strategy="SCALP_15M_v1", timeframe="15m"):
    return {"paper_order_id": identity, "status": "FILLED", "closed_at": None,
            "side": side, "symbol": "BTCUSDT", "strategy": strategy,
            "timeframe": timeframe, "position_size": 1000}


def context(s):
    return o.seal({"schema": "paper-risk-review-context-v1", "origin": o.DEFAULT_ORIGIN,
        "stable_runtime": True, "status_before": s["data"]["status"],
        "status_after": s["data"]["status"], "completed_at": s["completed_at"],
        "data": {"limits": {"max_gross_exposure_percent": 200},
                 "risk": {"portfolio": {"position_count": 0, "gross_exposure": 0, "net_exposure": 0}}}})


class PaperReadinessReviewTests(unittest.TestCase):
    def test_zero_directional_accuracy_is_unavailable(self):
        self.assertIsNone(r.wilson_interval(None, 0))

    def test_invalid_accuracy_and_sample_counts_fail(self):
        for accuracy, samples in [(1, 0), (None, 2), (float("nan"), 2), (1.1, 2), (0.8, True), (0.8, -1)]:
            with self.assertRaises(ValueError): r.wilson_interval(accuracy, samples)

    def test_five_of_six_is_not_strong_directional_evidence(self):
        low, high = r.wilson_interval(5 / 6, 6)
        self.assertAlmostEqual(low, 0.436497, places=5)
        self.assertGreater(high, 0.9)
        self.assertLess(low, 0.5)

    def test_larger_directional_sample_is_distinguished(self):
        self.assertGreater(r.wilson_interval(0.68, 50)[0], 0.5)

    def test_total_samples_cannot_substitute_for_directional_samples(self):
        row = r.review(snapshot())["agents"][0]
        self.assertEqual(row["total_samples"], 147)
        self.assertEqual(row["directional_samples"], 6)
        self.assertFalse(row["research_minimum_directional_met"])
        self.assertTrue(row["runtime_candidate"])

    def test_impossible_directional_accounting_is_rejected(self):
        s = snapshot();s["data"]["scorecards"]["scorecards"][0]["directional_sample_count"] = 148
        with self.assertRaises(ValueError): r.review(o.seal(s))

    def test_invalid_research_minimum_fails(self):
        with self.assertRaises(ValueError): r.agent_review(snapshot()["data"], True)

    def test_opposing_desks_keep_gross_exposure(self):
        s = snapshot();s["data"]["orders"]["orders"] = [order("a"), order("b", "SELL", "DAY_1H_v1", "1h")]
        s["data"]["performance"]["open_trades"] = 2
        result = r.review(o.seal(s))["portfolio"]
        self.assertEqual(result["gross_notional"], 2000)
        self.assertEqual(result["net_notional"], 0)
        self.assertEqual(result["mixed_side_symbols"], ["BTCUSDT"])
        self.assertEqual(result["by_timeframe"], {"15m": 1000, "1h": 1000})

    def test_duplicate_order_cannot_double_count_exposure(self):
        s = snapshot();s["data"]["orders"]["orders"] = [order("a"), order("a")]
        with self.assertRaises(ValueError): r.review(o.seal(s))

    def test_partial_fill_requires_reconciliation(self):
        s = snapshot();row = order("a");row["status"] = "PARTIALLY_FILLED"
        s["data"]["orders"]["orders"] = [row]
        with self.assertRaises(ValueError): r.review(o.seal(s))

    def test_open_count_mismatch_is_rejected(self):
        s = snapshot();s["data"]["performance"]["open_trades"] = 1
        with self.assertRaises(ValueError): r.review(o.seal(s))

    def test_closed_history_does_not_create_open_exposure(self):
        s = snapshot();row = order("a");row.update(status="CLOSED", closed_at=s["completed_at"])
        s["data"]["orders"]["orders"] = [row]
        self.assertEqual(r.review(o.seal(s))["portfolio"]["gross_notional"], 0)

    def test_nonfinite_exposure_fails_before_report(self):
        d = snapshot()["data"];row = order("a");row["position_size"] = float("inf")
        d["orders"]["orders"] = [row]
        with self.assertRaises(ValueError): r.book_review(d)

    def test_research_never_grants_phase_c_or_trading_authority(self):
        result = r.review(snapshot())
        self.assertFalse(result["phase_c_certified"])
        self.assertFalse(result["decision_authority"])
        self.assertFalse(result["automatic_weight_adjustment"])
        self.assertIsNone(result["execution_timing"]["order_latency_ms"])

    def test_tampered_or_nonpaper_snapshot_is_rejected(self):
        s = snapshot();s["data"]["status"]["mode"] = "LIVE"
        for value in (s, o.seal(s)):
            with self.assertRaises(ValueError): r.review(value)

    def test_report_does_not_reprice_net_pnl(self):
        result = r.review(snapshot())
        self.assertEqual(result["performance_as_reported"]["net_pnl"], -49.4088)

    def test_review_does_not_modify_immutable_inputs(self):
        s = snapshot();before = copy.deepcopy(s)
        r.review(s)
        self.assertEqual(s, before)

    def test_reconciled_risk_context_preserves_observed_limits(self):
        s = snapshot();c = context(s)
        result = r.review(s, c)
        self.assertEqual(result["risk_context"]["limits_as_reported"], c["data"]["limits"])

    def test_stale_risk_context_fails(self):
        s = snapshot();c = context(s);c["completed_at"] = "2026-10-28T01:19:00+00:00"
        with self.assertRaises(ValueError): r.review(s, o.seal(c))

    def test_divergent_risk_book_fails(self):
        s = snapshot();c = context(s);c["data"]["risk"]["portfolio"]["gross_exposure"] = 100
        with self.assertRaises(ValueError): r.review(s, o.seal(c))


if __name__ == "__main__":
    unittest.main()

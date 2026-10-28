"""Behavioral checks for the external read-only PAPER observer (stdlib only)."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "observe_paper_forward.py"
SPEC = importlib.util.spec_from_file_location("paper_forward_observer", SCRIPT)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def fixture():
    performance = {key: 0 for key in observer.COUNTERS + observer.COSTS}
    performance.update(open_trades=1, total_trades=1, net_pnl=0.0, gross_pnl=0.0,
                       balance=10000.0, initial_balance=10000.0, max_drawdown_percent=0.0)
    status = {
        "mode": "PAPER", "source_revision": observer.EXPECTED_REVISION,
        "kill_switch_active": False,
        "oms": {"environment": "PAPER", "live_execution_available": False},
        "release_evidence": {"status": "PASSED", "matches_runtime_revision": True,
                             "source_revision": observer.EXPECTED_REVISION},
        "runtime_capacity": {"market_subscriptions": sorted(observer.SUBSCRIPTIONS),
                             "database_pool_size": 4},
    }
    governance = {"decision_authority": False, "automatic_weight_adjustment": False}
    return {
        "status": status,
        "ready": {"status": "ready", "checks": dict.fromkeys(observer.READY_CHECKS, True)},
        "performance": performance,
        "orders": {"orders": [{"status": "FILLED", "closed_at": None}]},
        "strategies": {"strategies": [{"strategy_id": "SCALP_15M", "version": "v1",
                                        "enabled": True, "timeframe": "15m"}]},
        "orchestrator": {"cycle_latency_ms": {"samples": 24, "p95": 591}},
        "timeframes": {"overall": copy.deepcopy(performance), "breakdown_by": "timeframe",
                       "breakdown": [], "equity_curve": []},
        "scorecards": {**governance, "scorecards": [{"sample_count": 120}]},
        "candidates": {**governance, "candidates": [{"agent_name": "TrendAgent",
                       "eligible_for_regime_shadow_test": True}]},
        "regimes": {**governance, "rows": [], "shadow_specialist_count": 0,
                    "collecting_count": 1, "unavailable_historical_forecasts": 100},
    }


def collect(data=None):
    documents = fixture() if data is None else data
    paths = {path: key for key, path in observer.ENDPOINTS.items()}
    return observer.capture(observer.DEFAULT_ORIGIN,
                            getter=lambda origin, path: copy.deepcopy(documents[paths[path]]))


def closed_capture():
    documents = fixture()
    documents["performance"].update(open_trades=0, closed_trades=1, losses=1,
                                    net_pnl=-49.4088, gross_pnl=-33.4088, balance=9950.59,
                                    fees_total=16.0, slippage_total=16.183,
                                    total_execution_cost=32.183)
    documents["timeframes"]["overall"] = copy.deepcopy(documents["performance"])
    documents["timeframes"]["breakdown"] = [
        {"key": "15m", "trades": 1, "wins": 0, "losses": 1, "net_pnl": -49.4088}]
    return collect(documents)


class PaperForwardObservationTests(unittest.TestCase):
    def test_complete_capture_accepts_filled_open_position(self):
        snapshot = collect()
        self.assertEqual(snapshot["status"], "OBSERVING")
        self.assertEqual(snapshot["data"]["performance"]["open_trades"], 1)

    def test_scope_refused_before_fetching_orders(self):
        for change in ({"mode": "LIVE"}, {"source_revision": "wrong"},
                       {"oms": {"environment": "PAPER", "live_execution_available": True}}):
            data, calls = fixture(), []
            data["status"].update(change)
            def get(origin, path):
                calls.append(path)
                return data["status"]
            snapshot = observer.capture(observer.DEFAULT_ORIGIN, getter=get)
            self.assertEqual(snapshot["status"], "SCOPE_REFUSED")
            self.assertEqual(calls, [observer.ENDPOINTS["status"]])

    def test_unsafe_origins_are_rejected(self):
        for origin in ("http://example.com", "https://u:p@example.com",
                       "https://example.com/api", "https://example.com?token=x",
                       "https://example.com#fragment"):
            with self.assertRaises(ValueError):
                observer.validate_origin(origin)

    def test_network_requests_are_get_without_authorization(self):
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"success":true,"data":{"ok":true}}'
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response()
        with patch.object(observer, "build_opener", return_value=Opener()):
            self.assertEqual(observer.public_get(observer.DEFAULT_ORIGIN, "/ready"), {"ok": True})
        self.assertEqual(requests[0].method, "GET")
        self.assertFalse(requests[0].has_header("Authorization"))
        with self.assertRaises(ValueError):
            observer.public_get(observer.DEFAULT_ORIGIN, "/api/v1/paper/orders/execute")

    def test_redirects_are_refused(self):
        self.assertIsNone(observer.NoRedirect().redirect_request(None, None, 302, "", {},
                                                                 "https://other.invalid"))

    def test_missing_readiness_check_needs_attention(self):
        data = fixture()
        del data["ready"]["checks"]["broker"]
        self.assertEqual(collect(data)["status"], "ATTENTION")

    def test_duplicate_extra_subscription_needs_attention(self):
        data = fixture()
        data["status"]["runtime_capacity"]["market_subscriptions"].append(("BTCUSDT", "15m"))
        self.assertFalse(collect(data)["checks"]["nine_subscriptions"])

    def test_missing_or_invalid_metrics_are_not_zero_filled(self):
        for value in (None, "0", True):
            data = fixture()
            data["performance"]["closed_trades"] = value
            self.assertFalse(collect(data)["checks"]["performance_usable"])

    def test_sequential_performance_race_is_flagged(self):
        data = fixture()
        data["timeframes"]["overall"]["open_trades"] = 0
        self.assertFalse(collect(data)["checks"]["consistent_performance_reads"])

    def test_runtime_change_during_capture_is_flagged(self):
        data, calls = fixture(), []
        paths = {path: key for key, path in observer.ENDPOINTS.items()}
        def get(origin, path):
            result = copy.deepcopy(data[paths[path]])
            if path == observer.ENDPOINTS["status"]:
                calls.append(path)
                if len(calls) > 1:
                    result["mode"] = "LIVE"
            return result
        self.assertEqual(observer.capture(observer.DEFAULT_ORIGIN, getter=get)["status"], "ATTENTION")

    def test_partial_errors_redact_arbitrary_messages(self):
        data = fixture()
        paths = {path: key for key, path in observer.ENDPOINTS.items()}
        def get(origin, path):
            if path == observer.ENDPOINTS["orders"]:
                raise RuntimeError("private-token-do-not-store")
            return copy.deepcopy(data[paths[path]])
        snapshot = observer.capture(observer.DEFAULT_ORIGIN, getter=get)
        self.assertEqual(snapshot["status"], "COLLECTION_INCOMPLETE")
        self.assertNotIn("private-token", json.dumps(snapshot))

    def test_report_authority_cannot_be_enabled(self):
        data = fixture()
        data["candidates"]["decision_authority"] = True
        self.assertEqual(collect(data)["status"], "ATTENTION")

    def test_zero_closed_trades_has_no_invented_win_rate(self):
        baseline, current = collect(), collect()
        result = observer.compare(baseline, current)
        self.assertTrue(result["comparable"])
        self.assertIsNone(result["win_rate_percent"])

    def test_net_pnl_does_not_subtract_costs_twice(self):
        result = observer.compare(collect(), closed_capture())
        self.assertTrue(result["comparable"])
        self.assertEqual(result["reported_realized_net_pnl"], -49.4088)
        self.assertEqual(result["cost_deltas"]["total_execution_cost"], 32.183)
        self.assertIn("before baseline", result["pnl_scope"])

    def test_counter_reset_invalidates_comparison(self):
        baseline, current = closed_capture(), collect()
        self.assertIn("cumulative_counter_reset", observer.compare(baseline, current)["reasons"])

    def test_capital_or_unreconciled_cash_change_invalidates_comparison(self):
        for field in ("initial_balance", "balance"):
            baseline, current = collect(), collect()
            current["data"]["performance"][field] += 100
            self.assertFalse(observer.compare(baseline, current)["comparable"])

    def test_capacity_and_strategy_changes_invalidate_window(self):
        for key in ("capacity", "strategy"):
            baseline, current = collect(), collect()
            if key == "capacity":
                current["data"]["status"]["runtime_capacity"]["database_pool_size"] = 9
            else:
                current["data"]["strategies"]["strategies"][0]["enabled"] = False
            self.assertFalse(observer.compare(baseline, current)["comparable"])

    def test_ambiguous_timeframe_is_not_labeled_one_strategy(self):
        baseline, current = collect(), closed_capture()
        extra = {"strategy_id": "SECOND", "version": "v1", "enabled": True, "timeframe": "15m"}
        for snapshot in (baseline, current):
            snapshot["data"]["strategies"]["strategies"].append(copy.deepcopy(extra))
        self.assertIsNone(observer.compare(baseline, current)["timeframe_deltas"][0]["strategy"])

    def test_agent_history_is_not_claimed_as_campaign_evidence(self):
        result = collect()["agents"]
        self.assertEqual(result["eligible_candidate_count"], 1)
        self.assertIn("predate", result["sample_scope"])
        self.assertIn("not causal", result["interpretation"])

    def test_comparison_does_not_mutate_input(self):
        baseline, current = collect(), closed_capture()
        before = copy.deepcopy((baseline, current))
        observer.compare(baseline, current)
        self.assertEqual((baseline, current), before)

    def test_immutable_files_detect_tampering_and_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            observer.write_snapshot(path, collect())
            self.assertEqual(observer.load_snapshot(path)["status"], "OBSERVING")
            with self.assertRaises(FileExistsError):
                observer.write_snapshot(path, collect())
            value = json.loads(path.read_text())
            value["status"] = "forged"
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                observer.load_snapshot(path)
            path.write_text('{"number": NaN}')
            with self.assertRaises(ValueError):
                observer.load_snapshot(path)

    def test_bad_source_or_collector_or_overlapping_window_is_incomparable(self):
        for field in ("origin", "expected_revision", "collector_sha256", "started_at"):
            baseline, current = collect(), collect()
            current[field] = "2020-01-01T00:00:00+00:00" if field == "started_at" else "changed"
            self.assertFalse(observer.compare(baseline, current)["comparable"])


if __name__ == "__main__":
    unittest.main()

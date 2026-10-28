"""Collect private, immutable PAPER evidence using public GET reports only.

Stdlib-only external observer. It never imports the application, requests a
credential, sends an order, changes a setting, or promotes an agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SCHEMA = "paper-forward-observation-v1"
DEFAULT_ORIGIN = "https://capital-cipher-backend-staging.up.railway.app"
EXPECTED_REVISION = "ec882651ac0a098891420d6159f0f9ba42a05f5b"
READY_CHECKS = (
    "database", "broker", "paper_execution_boundary", "agent_cohort",
    "operations_monitor", "market_data", "kill_switch_clear", "system_state",
)
SUBSCRIPTIONS = {
    (symbol, timeframe)
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    for timeframe in ("15m", "1h", "4h")
}
ENDPOINTS = {
    "status": "/api/v1/status",
    "ready": "/ready",
    "performance": "/api/v1/paper/performance",
    "orders": "/api/v1/paper/orders",
    "strategies": "/api/v1/strategies",
    "orchestrator": "/api/v1/orchestrator/status",
    "timeframes": "/api/v1/reports/performance?by=timeframe",
    "scorecards": "/api/v1/reports/agents/specialists",
    "candidates": "/api/v1/reports/agents/specialist-candidates",
    "regimes": "/api/v1/reports/agents/regime-shadow",
}
COUNTERS = ("total_trades", "closed_trades", "wins", "losses", "liquidations")
COSTS = (
    "fees_total", "slippage_total", "spread_total", "volume_impact_total",
    "funding_total", "liquidation_fees_total", "total_execution_cost",
)
MONEY = ("net_pnl", "gross_pnl", "balance", "initial_balance") + COSTS


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(payload).hexdigest()


def seal(snapshot: dict) -> dict:
    result = {key: value for key, value in snapshot.items() if key != "sha256"}
    return {**result, "sha256": digest(result)}


def reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


def load_snapshot(path: Path) -> dict:
    snapshot = json.loads(path.read_text(), parse_constant=reject_constant)
    if snapshot.get("schema") != SCHEMA or snapshot.get("sha256") != seal(snapshot)["sha256"]:
        raise ValueError("Invalid snapshot schema or digest")
    return snapshot


def write_snapshot(path: Path, snapshot: dict) -> None:
    # Exclusive creation prevents silently resetting a baseline or a capture.
    payload = json.dumps(seal(snapshot), ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as output:
        output.write(payload + "\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTPS origin without credentials, path, query or fragment")
    return origin.rstrip("/")


def public_get(origin: str, path: str) -> dict:
    if path not in ENDPOINTS.values():
        raise ValueError("Endpoint is not in the read-only allowlist")
    request = Request(validate_origin(origin) + path, method="GET",
                      headers={"Accept": "application/json"})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError("Expected HTTP 200")
        payload = response.read(5_000_001)
    if len(payload) > 5_000_000:
        raise ValueError("Response exceeds evidence size limit")
    document = json.loads(payload, parse_constant=reject_constant)
    if not isinstance(document, dict) or document.get("success") is False:
        raise ValueError("Report unavailable")
    data = document.get("data", document)
    if not isinstance(data, dict):
        raise ValueError("Expected report object")
    return data


def paper_scope(status: dict, revision: str) -> bool:
    oms = status.get("oms", {})
    return (status.get("mode") == "PAPER" and oms.get("environment") == "PAPER"
            and oms.get("live_execution_available") is False
            and status.get("source_revision") == revision)


def number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_metrics(metrics: dict) -> bool:
    integers = COUNTERS + ("open_trades",)
    return (isinstance(metrics, dict)
            and all(type(metrics.get(k)) is int and metrics[k] >= 0 for k in integers)
            and all(number(metrics.get(k)) for k in MONEY + ("max_drawdown_percent",))
            and metrics["initial_balance"] > 0
            and metrics["total_trades"] == metrics["open_trades"] + metrics["closed_trades"]
            and metrics["closed_trades"] == metrics["wins"] + metrics["losses"])


def checks_for(data: dict, revision: str) -> dict:
    status, ready = data["status"], data["ready"]
    capacity = status.get("runtime_capacity", {})
    subscriptions = capacity.get("market_subscriptions", [])
    performance = data["performance"]
    report = data["timeframes"]
    strategy_rows = data["strategies"].get("strategies")
    rows = report.get("breakdown")
    try:
        subscriptions_ok = (len(subscriptions) == 9
                            and {tuple(item) for item in subscriptions} == SUBSCRIPTIONS)
    except (TypeError, ValueError):
        subscriptions_ok = False
    breakdown_ok = (isinstance(rows, list) and all(
        isinstance(row, dict) and isinstance(row.get("key"), str)
        and all(type(row.get(k)) is int and row[k] >= 0 for k in ("trades", "wins", "losses"))
        and row["trades"] == row["wins"] + row["losses"]
        and number(row.get("net_pnl")) for row in rows))
    if breakdown_ok and valid_metrics(performance):
        breakdown_ok = (len({row["key"] for row in rows}) == len(rows)
                        and sum(row["trades"] for row in rows) == performance["closed_trades"]
                        and abs(sum(row["net_pnl"] for row in rows) - performance["net_pnl"])
                        <= 0.0001 * (len(rows) + 1))
    release = status.get("release_evidence", {})
    return {
        "paper_scope": paper_scope(status, revision),
        "ready": ready.get("status") == "ready" and all(
            ready.get("checks", {}).get(key) is True for key in READY_CHECKS),
        "kill_switch_clear": status.get("kill_switch_active") is False,
        "release_matches": release.get("matches_runtime_revision") is True
        and release.get("source_revision") == revision and release.get("status") == "PASSED",
        "nine_subscriptions": subscriptions_ok,
        "performance_usable": valid_metrics(performance),
        "consistent_performance_reads": performance == report.get("overall"),
        "timeframe_breakdown_usable": report.get("breakdown_by") == "timeframe" and breakdown_ok,
        "strategies_usable": isinstance(strategy_rows, list) and bool(strategy_rows) and all(
            isinstance(row, dict) and isinstance(row.get("strategy_id"), str)
            and isinstance(row.get("version"), str) and isinstance(row.get("timeframe"), str)
            and type(row.get("enabled")) is bool for row in strategy_rows),
        "orders_usable": isinstance(data["orders"].get("orders"), list),
        "agent_reports_usable": isinstance(data["scorecards"].get("scorecards"), list)
        and isinstance(data["candidates"].get("candidates"), list)
        and isinstance(data["regimes"].get("rows"), list)
        and all(data[name].get("decision_authority") is False
                and data[name].get("automatic_weight_adjustment") is False
                for name in ("scorecards", "candidates", "regimes")),
        "stable_runtime_during_capture": data["status_end"] == status,
    }


def agent_summary(data: dict) -> dict:
    candidates = data["candidates"]["candidates"]
    eligible = sorted(row["agent_name"] for row in candidates
                      if row.get("eligible_for_regime_shadow_test") is True)
    regime = data["regimes"]
    return {
        "scorecard_count": len(data["scorecards"]["scorecards"]),
        "eligible_candidate_names": eligible,
        "eligible_candidate_count": len(eligible),
        "qualified_regime_specialists": regime.get("shadow_specialist_count"),
        "collecting_regime_rows": regime.get("collecting_count"),
        "unavailable_historical_forecasts": regime.get("unavailable_historical_forecasts"),
        "sample_scope": "cumulative history, may predate this campaign; evaluated_at is report time",
        "interpretation": "observational forecast scores, not causal strategy PnL or promotion authority",
    }


def capture(origin: str, revision: str = EXPECTED_REVISION,
            getter: Callable[[str, str], dict] = public_get) -> dict:
    origin = validate_origin(origin)
    result = {"schema": SCHEMA, "origin": origin, "expected_revision": revision,
              "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "started_at": utcnow(), "status": "COLLECTION_INCOMPLETE",
              "data": {}, "errors": {}, "checks": {}}
    for name, path in ENDPOINTS.items():
        try:
            result["data"][name] = getter(origin, path)
        except Exception as error:
            # Arbitrary server/exception messages may contain private details.
            result["errors"][name] = {"type": type(error).__name__}
            if isinstance(error, HTTPError):
                result["errors"][name]["http_status"] = error.code
        if name == "status":
            if name in result["errors"]:
                break
            if not paper_scope(result["data"][name], revision):
                result["status"] = "SCOPE_REFUSED"
                break
    if not result["errors"] and result["status"] != "SCOPE_REFUSED":
        try:
            result["data"]["status_end"] = getter(origin, ENDPOINTS["status"])
            result["checks"] = checks_for(result["data"], revision)
            result["status"] = "OBSERVING" if all(result["checks"].values()) else "ATTENTION"
            if result["checks"]["agent_reports_usable"]:
                result["agents"] = agent_summary(result["data"])
        except Exception as error:
            result["status"] = "COLLECTION_INCOMPLETE"
            result["errors"]["validation"] = {"type": type(error).__name__}
    result["completed_at"] = utcnow()
    result["limitations"] = [
        "OBSERVING means usable technical evidence, not financial success.",
        "GET reports are sequential, not a transactional snapshot; retry inconsistent reads.",
        "Orders report contains open positions plus only the latest 100 closed positions.",
        "Cycle latency is a rolling orchestrator metric, not order latency or a production SLO.",
        "Public runtime and strategy fingerprints cannot detect all hidden risk/environment changes.",
    ]
    return seal(result)


def compare(baseline: dict, current: dict) -> dict:
    reasons = []
    for label in ("origin", "expected_revision", "collector_sha256"):
        if baseline.get(label) != current.get(label):
            reasons.append(label + "_changed")
    for label, snapshot in (("baseline", baseline), ("current", current)):
        if snapshot.get("status") != "OBSERVING":
            reasons.append(label + "_not_usable")
    if reasons:
        return {"comparable": False, "reasons": reasons}
    if datetime.fromisoformat(current["started_at"]) < datetime.fromisoformat(baseline["completed_at"]):
        reasons.append("overlapping_or_reversed_capture_window")
    b, c = baseline["data"], current["data"]
    for label, left, right in (
        ("runtime_capacity", b["status"].get("runtime_capacity"), c["status"].get("runtime_capacity")),
        ("strategies", b["strategies"], c["strategies"]),
    ):
        if digest(left) != digest(right):
            reasons.append(label + "_changed")
    before, after = b["performance"], c["performance"]
    if before["initial_balance"] != after["initial_balance"]:
        reasons.append("initial_capital_changed")
    if any(after[key] < before[key] for key in COUNTERS):
        reasons.append("cumulative_counter_reset")
    if abs((after["balance"] - before["balance"]) - (after["net_pnl"] - before["net_pnl"])) > 0.021:
        reasons.append("balance_delta_not_reconciled")
    b_rows = {row["key"]: row for row in b["timeframes"]["breakdown"]}
    c_rows = {row["key"]: row for row in c["timeframes"]["breakdown"]}
    deltas = []
    for timeframe in sorted(b_rows.keys() | c_rows.keys()):
        left, right = b_rows.get(timeframe, {}), c_rows.get(timeframe, {})
        row = {key: round(right.get(key, 0) - left.get(key, 0), 4)
               for key in ("trades", "wins", "losses", "net_pnl")}
        if any(row[key] < 0 for key in ("trades", "wins", "losses")):
            reasons.append("timeframe_counter_reset")
        matches = [s for s in c["strategies"]["strategies"]
                   if s["enabled"] and s["timeframe"] == timeframe]
        row.update(timeframe=timeframe, strategy=(matches[0]["strategy_id"] + "_" + matches[0]["version"])
                   if len(matches) == 1 else None,
                   attribution="unique enabled strategy for timeframe; historical configuration assumed"
                   if len(matches) == 1 else "ambiguous strategy attribution")
        deltas.append(row)
    if reasons:
        return {"comparable": False, "reasons": sorted(set(reasons))}
    closed = after["closed_trades"] - before["closed_trades"]
    wins = after["wins"] - before["wins"]
    return {
        "comparable": True, "baseline_sha256": baseline["sha256"],
        "closed_trades": closed, "wins": wins,
        "losses": after["losses"] - before["losses"],
        "win_rate_percent": round(wins / closed * 100, 2) if closed else None,
        "reported_realized_net_pnl": round(after["net_pnl"] - before["net_pnl"], 4),
        "reported_gross_pnl": round(after["gross_pnl"] - before["gross_pnl"], 4),
        "cost_deltas": {key: round(after[key] - before[key], 4) for key in COSTS},
        "timeframe_deltas": deltas,
        "runtime_max_drawdown_percent": after["max_drawdown_percent"],
        "campaign_max_drawdown_percent": None,
        "pnl_scope": "realized closes during window, including positions opened before baseline",
        "cost_policy": "use reported net PnL directly; do not subtract slippage or fees a second time",
        "drawdown_scope": "runtime realized history only; campaign mark-to-market drawdown unavailable",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--expected-revision", default=EXPECTED_REVISION)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    baseline = load_snapshot(args.baseline) if args.baseline else None
    snapshot = capture(args.origin, args.expected_revision)
    if baseline is not None:
        snapshot["comparison"] = compare(baseline, snapshot)
    write_snapshot(args.output, snapshot)
    print(json.dumps({"status": snapshot["status"], "checks": snapshot["checks"],
                      "errors": snapshot["errors"], "agents": snapshot.get("agents"),
                      "comparison": snapshot.get("comparison"), "output": str(args.output)}))
    return 0 if snapshot["status"] == "OBSERVING" and snapshot.get("comparison", {}).get("comparable", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline economics of frozen S2/S2C; no selection, policy changes or orders.

Baseline trade outcomes classified by the filter are descriptive counterfactuals.
They are NOT the marginal value of removing a rule: occupancy, capital and daily
limits change after a veto. The exact account PnL bridge preserves those effects.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from math import fsum, isclose

from app.strategy.gate_classical_s2 import evaluate_strategy
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2c_confluence import (
    HistoricalConfluenceEvaluator, evaluate_confluence,
)


def trade_key(trade):
    return trade.decision_bar_index, trade.side


def pnl_bridge(baseline, filtered):
    """Exact bridge between independent account paths, matched by decision/side."""
    left = {trade_key(t): t for t in baseline.trades}
    right = {trade_key(t): t for t in filtered.trades}
    if len(left) != len(baseline.trades) or len(right) != len(filtered.trades):
        raise ValueError("Duplicate trade decision identity")
    if (baseline.strategy_id != filtered.strategy_id
            or baseline.start_equity != filtered.start_equity):
        raise ValueError("Incomparable research accounts")
    common = left.keys() & right.keys()
    left_only = left.keys() - right.keys()
    right_only = right.keys() - left.keys()
    common_delta = fsum(right[k].net_pnl-left[k].net_pnl for k in common)
    removed_net = fsum(left[k].net_pnl for k in left_only)
    added_net = fsum(right[k].net_pnl for k in right_only)
    total = common_delta + added_net - removed_net
    expected = filtered.net_pnl-baseline.net_pnl
    if not isclose(total, expected, abs_tol=1e-6, rel_tol=1e-9):
        raise ValueError("Account bridge does not reconcile")
    return {
        "common_trade_decisions": len(common),
        "baseline_only_trade_decisions": len(left_only),
        "filtered_only_trade_decisions": len(right_only),
        "common_decision_net_difference": common_delta,
        "baseline_only_net_pnl": removed_net,
        "filtered_only_net_pnl": added_net,
        "filtered_minus_baseline_net_pnl": total,
        "identity": "common_difference + filtered_only - baseline_only",
        "marginal_filter_effect_identified": False,
    }


class TracedConfluence(HistoricalConfluenceEvaluator):
    def __init__(self, higher):
        super().__init__(higher)
        self.decisions = []

    def verdict(self, strategy, prefix, cutoff):
        plan = evaluate_strategy(strategy, prefix, as_of=cutoff,
                                 historical_replay=True)
        if plan.action == "WAIT":
            raise ValueError("Expected a filled baseline entry proposal")
        return evaluate_confluence(plan, self._time_locked_chart(prefix, cutoff))

    def evaluate(self, strategy, prefix, cutoff):
        # Execute the original frozen evaluator, then observe its verdict.
        result = super().evaluate(strategy, prefix, cutoff)
        proposal = evaluate_strategy(strategy, prefix, as_of=cutoff,
                                     historical_replay=True)
        if proposal.action != "WAIT":
            verdict = self.verdict(strategy, prefix, cutoff)
            if verdict.filtered_action != result.action:
                raise ValueError("Trace differs from frozen policy")
            self.decisions.append({
                "decision_bar_index": len(prefix)-1,
                "verdict": asdict(verdict),
            })
        return result


def _arm(report, friction):
    costs = asdict(attribute(report, assumptions=friction))
    return {
        "trades": len(report.trades), "net_pnl": report.net_pnl,
        "net_expectancy_per_trade": costs["net_expectancy_per_trade"],
        "profit_factor": report.profit_factor,
        "closed_equity_max_drawdown_pct": report.max_drawdown_pct,
        "estimated_boundary_exits": report.estimated_end_window_exits,
        "net_pnl_excluding_boundary_marks": fsum(
            t.net_pnl for t in report.trades
            if t.exit_reason != "END_WINDOW_ESTIMATE"),
        "blocked_new_entries": report.blocked_new_entries,
        "cost_attribution": costs,
    }


def _outcomes(trades):
    return {
        "trades": len(trades),
        "winners": sum(t.net_pnl > 0 for t in trades),
        "losers": sum(t.net_pnl < 0 for t in trades),
        "net_pnl": fsum(t.net_pnl for t in trades),
        "estimated_boundary_exits": sum(
            t.exit_reason == "END_WINDOW_ESTIMATE" for t in trades),
    }


def diagnose_pair(groups, strategy, *, start_index=None):
    if set(groups) != {"15m", "1h", "4h"} or not groups["15m"]:
        raise ValueError("Exactly three contexts required")
    primary = groups["15m"]
    symbol = primary[0].symbol
    for tf, step in (("15m", 900), ("1h", 3600), ("4h", 14400)):
        rows = groups[tf]
        if (not rows or any(
                r.symbol != symbol or r.timeframe != tf
                or r.exchange.value != "GATE" or r.received_at < r.closed_at
                for r in rows)
                or any((b.closed_at-a.closed_at).total_seconds() != step
                       for a, b in zip(rows, rows[1:]))):
            raise ValueError("Invalid snapshot provenance or chronology")
    start = int(len(primary)*.6) if start_index is None else start_index
    friction, risk = Friction(6, 6, 1), ResearchRisk()
    kwargs = dict(start_index=start, end_index=len(primary),
                  segment="REUSED_HISTORY_EXPLORATORY", friction=friction, risk=risk)
    baseline = replay_segment(strategy, primary, **kwargs)
    tracer = TracedConfluence({tf: groups[tf] for tf in ("1h", "4h")})
    filtered = replay_segment(strategy, primary, plan_evaluator=tracer.evaluate,
                              **kwargs)
    baseline_rows = []
    buckets = {"ALLOWED": [], "VETOED": []}
    reason_trades = {}
    right_keys = {trade_key(t) for t in filtered.trades}
    allowed_but_absent = []
    for trade in baseline.trades:
        i = trade.decision_bar_index
        cutoff = primary[i].closed_at+timedelta(microseconds=1)
        verdict = tracer.verdict(strategy, primary[:i+1], cutoff)
        allowed = verdict.filtered_action != "WAIT"
        buckets["ALLOWED" if allowed else "VETOED"].append(trade)
        if allowed and trade_key(trade) not in right_keys:
            allowed_but_absent.append(trade)
        for reason in verdict.reasons:
            reason_trades.setdefault(reason, []).append(trade)
        baseline_rows.append({"trade": asdict(trade), "verdict": asdict(verdict)})
    summary = {
        "symbol": symbol, "strategy": strategy,
        "decision_frame": "15m", "context_frames": ["1h", "4h"],
        "decision_start_index": start, "candles_15m": len(primary),
        "first_decision_close_utc": primary[start].closed_at,
        "last_candle_close_utc": primary[-1].closed_at,
        "friction": asdict(friction), "risk": asdict(risk),
        "baseline": _arm(baseline, friction),
        "filtered": _arm(filtered, friction),
        "cash_net_pnl_per_account": 0.0,
        "account_bridge": pnl_bridge(baseline, filtered),
        "baseline_trade_outcomes_by_filter_verdict": {
            k: _outcomes(v) for k, v in buckets.items()},
        "baseline_allowed_but_absent_in_filtered_path": _outcomes(allowed_but_absent),
        "baseline_veto_reasons_nonadditive": {
            k: _outcomes(v) for k, v in sorted(reason_trades.items())},
        "filtered_path_proposal_counts": dict(sorted(tracer.counts.items())),
        "pristine_holdout": False, "candidate_selected": False,
        "profitability_proven": False, "execution_authorized": False,
        "policy_mutation_authorized": False,
    }
    return {"summary": summary, "baseline_trade_trace": baseline_rows,
            "filtered_trade_trace": [asdict(t) for t in filtered.trades],
            "filtered_path_proposal_trace": tracer.decisions}

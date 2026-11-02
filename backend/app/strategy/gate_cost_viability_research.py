"""Isolated fixed cost-viability hypotheses; never loaded by services or OMS.

Target/cost is a pre-decision price-distance estimate, not expected profit.
Stops, targets, holding limits and position sizing belong to unchanged S2.
Cached decisions are computed only from the supplied historical closed prefix.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from math import ceil, fsum, isfinite

from app.strategy.gate_classical_s2 import (
    MIN_HISTORY, STRATEGIES, TIMEFRAME_SECONDS, _check_bars, evaluate_strategy,
)
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment
from app.strategy.gate_s2_loss_diagnostics import attribute
from app.strategy.gate_s2c_confluence import HistoricalConfluenceEvaluator, evaluate_confluence

BASE_FRICTION = Friction(6, 6, 1)
STRESS_FRICTION = Friction(6, 12, 2)
MULTIPLES = {"COST_2X": 2, "COST_4X": 4,
             "S1_COST_2X": 2, "S1_COST_4X": 4}


def variants(timeframe):
    basic = ("BASELINE", "COST_2X", "COST_4X")
    return basic+("S1", "S1_COST_2X", "S1_COST_4X") if timeframe == "15m" else basic


def estimated_round_trip_cost(plan, last_price, *, assumptions=BASE_FRICTION):
    """Conservative max-hold funding budget, per unit, from known last close.

    Actual gaps, execution prices, holding duration and exchange fills remain
    unknown. No next-open or future return is used to permit an entry.
    """
    if (plan.execution_authorized is not False or plan.predictive_edge_proven is not False
            or plan.strategy_id not in STRATEGIES
            or plan.strategy_version != STRATEGIES[plan.strategy_id].version
            or plan.timeframe not in TIMEFRAME_SECONDS
            or plan.action not in ("LONG", "SHORT")
            or not isfinite(last_price) or last_price <= 0
            or plan.stop_distance is None or plan.target_distance is None
            or not all(isfinite(x) and x > 0
                       for x in (plan.stop_distance, plan.target_distance))):
        raise ValueError("Invalid or executable cost research proposal")
    holding_seconds = STRATEGIES[plan.strategy_id].max_hold_bars*TIMEFRAME_SECONDS[plan.timeframe]
    funding_intervals = max(1, ceil(holding_seconds/(8*3600)))
    bps = 2*(assumptions.fee_bps_side+assumptions.slippage_bps_side)
    bps += funding_intervals*assumptions.funding_bps_8h
    return last_price*bps/10000


def filter_cost(plan, last_price, multiple):
    if multiple not in (2, 4):
        raise ValueError("Only the two predeclared cost hypotheses are allowed")
    if plan.action == "WAIT":
        if plan.execution_authorized is not False or plan.predictive_edge_proven is not False:
            raise ValueError("Executable WAIT forbidden")
        return plan
    cost = estimated_round_trip_cost(plan, last_price)
    if plan.target_distance >= multiple*cost:
        return plan
    return replace(plan, action="WAIT", stop_distance=None, target_distance=None,
                   reasoning=f"COST_VIABILITY_V1_REJECTED_{multiple}X")


class DecisionBook:
    """Pure cached features shared across independent replay accounts."""
    def __init__(self, primary, higher=None):
        self.primary = tuple(c.model_copy(deep=True) for c in primary)
        if not self.primary:
            raise ValueError("Empty research input")
        _check_bars(self.primary, self.primary[-1].closed_at+timedelta(microseconds=1), replay=True)
        higher = ({tf: tuple(c.model_copy(deep=True) for c in rows)
                   for tf, rows in higher.items()} if higher is not None else None)
        self.context = HistoricalConfluenceEvaluator(higher) if higher is not None else None
        if self.context is not None:
            if self.primary[0].timeframe != "15m":
                raise ValueError("S1 adapter is frozen to 15m entries")
            for rows in higher.values():
                _check_bars(rows, rows[-1].closed_at+timedelta(microseconds=1), replay=True)
                if rows[0].symbol != self.primary[0].symbol:
                    raise ValueError("Mixed context symbol")
        self.plans = {}
        self.charts = {}
        self.verdicts = {}

    def evaluate(self, strategy, prefix, cutoff, variant):
        if variant not in variants(self.primary[0].timeframe):
            raise ValueError("Unsupported research variant")
        i = len(prefix)-1
        if (i >= len(self.primary) or i < MIN_HISTORY-1
                or tuple(prefix) != self.primary[:i+1]
                or cutoff != prefix[-1].closed_at+timedelta(microseconds=1)):
            raise ValueError("Decision prefix or clock changed")
        key = (strategy, i)
        if key not in self.plans:
            self.plans[key] = evaluate_strategy(strategy, prefix, as_of=cutoff,
                                                 historical_replay=True)
        plan = self.plans[key]
        if plan.action == "WAIT":
            return plan
        if variant.startswith("S1"):
            if self.context is None:
                raise ValueError("Missing frozen S1 context")
            if i not in self.charts:
                self.charts[i] = self.context._time_locked_chart(prefix, cutoff)
            if key not in self.verdicts:
                self.verdicts[key] = evaluate_confluence(plan, self.charts[i])
            verdict = self.verdicts[key]
            if verdict.filtered_action == "WAIT":
                return replace(plan, action="WAIT", stop_distance=None, target_distance=None,
                               reasoning="FROZEN_S1_REJECTED: "+",".join(verdict.reasons))
        if variant in MULTIPLES:
            plan = filter_cost(plan, prefix[-1].close, MULTIPLES[variant])
        return plan

    def run(self, strategy, variant, start, end, *, friction=BASE_FRICTION):
        # Truncation prevents even validation/receipt checks from reading beyond
        # the current train/forward endpoint. Histories assume hypothetical clocks.
        return replay_segment(
            strategy, self.primary[:end], start_index=start, end_index=end,
            segment="COST_VIABILITY_REUSED_HISTORY", friction=friction, risk=ResearchRisk(),
            plan_evaluator=lambda s, p, c: self.evaluate(s, p, c, variant),
        )


def metrics(report, friction):
    completed = [t for t in report.trades if t.exit_reason != "END_WINDOW_ESTIMATE"]
    wins = fsum(t.net_pnl for t in completed if t.net_pnl > 0)
    losses = fsum(-t.net_pnl for t in completed if t.net_pnl < 0)
    closed_net = fsum(t.net_pnl for t in completed)
    return {
        "trades_including_boundary_marks": len(report.trades),
        "completed_trades": len(completed),
        "boundary_marks": report.estimated_end_window_exits,
        "net_including_boundary_marks": report.net_pnl,
        "completed_trade_net": closed_net,
        "completed_trade_expectancy": closed_net/len(completed) if completed else None,
        "completed_trade_profit_factor": wins/losses if losses else None,
        "all_completed_trades_profitable": bool(completed) and not losses,
        "closed_equity_max_drawdown_pct": report.max_drawdown_pct,
        "blocked_new_entries": report.blocked_new_entries,
        "cost_attribution_including_boundary_marks": asdict(attribute(report, assumptions=friction)),
    }


def choose_on_training(training):
    """No boundary marks, future returns or stress results may select a rule."""
    controls = [training[k]["completed_trade_net"] for k in ("BASELINE", "S1") if k in training]
    hurdle = max(0., *controls)
    eligible = []
    for variant, result in training.items():
        if variant not in MULTIPLES:
            continue
        pf = result["completed_trade_profit_factor"]
        if (result["completed_trades"] >= 5 and result["completed_trade_net"] > hurdle
                and (pf is not None and pf > 1 or result["all_completed_trades_profitable"])):
            eligible.append(variant)
    return min(eligible, key=lambda k: (-training[k]["completed_trade_net"], k)) if eligible else None


def walk_forward(book, strategy, *, initial_train=400, forward_bars=120):
    if initial_train < MIN_HISTORY+30 or forward_bars < 96:
        raise ValueError("Insufficient chronological research windows")
    if len(book.primary) < initial_train+forward_bars:
        raise ValueError("No complete forward fold")
    folds = []
    available = variants(book.primary[0].timeframe)
    for train_end in range(initial_train, len(book.primary)-forward_bars+1, forward_bars):
        end = train_end+forward_bars
        training = {v: metrics(book.run(strategy, v, MIN_HISTORY, train_end), BASE_FRICTION)
                    for v in available}
        selected = choose_on_training(training)
        comparisons = {}
        for cost_name, friction in (("base", BASE_FRICTION), ("stress", STRESS_FRICTION)):
            names = [v for v in ("BASELINE", "S1") if v in available]
            if selected is not None:
                names.append(selected)
            reports = {v: book.run(strategy, v, train_end, end, friction=friction) for v in names}
            stats = {v: metrics(r, friction) for v, r in reports.items()}
            comparisons[cost_name] = {
                "arms": stats,
                "arm_trade_traces": {v: [asdict(t) for t in r.trades] for v, r in reports.items()},
                "selected_net_or_cash": stats[selected]["completed_trade_net"] if selected else 0.,
                "selected_completed_trades": stats[selected]["completed_trades"] if selected else 0,
            }
        required = ["BASELINE"]+(["S1"] if "S1" in available else [])
        sufficient = selected is not None and all(
            comparisons[c]["arms"][v]["completed_trades"] >= 30
            for c in ("base", "stress") for v in [*required, selected])
        beats_controls = selected is not None and all(
            comparisons[c]["selected_net_or_cash"] > max(
                0., *(comparisons[c]["arms"][v]["completed_trade_net"] for v in required))
            for c in ("base", "stress"))
        folds.append({
            "train_end_exclusive": train_end, "forward_decision_start_index": train_end,
            "forward_end_exclusive": end, "training_arms": training,
            "selected_variant": selected, "selection_status": "SELECTED_FOR_DIAGNOSTIC" if selected else "ABSTAIN_NO_TRAIN_EDGE",
            "selection_uses_forward_data": False, "stress_policy_retuned": False,
            "comparisons": comparisons,
            "sufficient_completed_trades_each_arm": sufficient,
            "beats_cash_and_controls_after_costs_and_stress": beats_controls,
            "descriptive_screen_positive": bool(sufficient and beats_controls),
            "pristine_holdout": False, "profitability_proven": False,
        })
    return folds


def experiment_case(groups, strategy, timeframe):
    book = DecisionBook(groups[timeframe],
                        {tf: groups[tf] for tf in ("1h", "4h")} if timeframe == "15m" else None)
    start = int(len(book.primary)*.6)
    fixed = {}
    for cost_name, friction in (("base", BASE_FRICTION), ("stress", STRESS_FRICTION)):
        fixed[cost_name] = {}
        for variant in variants(timeframe):
            report = book.run(strategy, variant, start, len(book.primary), friction=friction)
            fixed[cost_name][variant] = {"metrics": metrics(report, friction),
                                        "trade_trace": [asdict(t) for t in report.trades]}
    folds = walk_forward(book, strategy)
    return {
        "symbol": book.primary[0].symbol, "timeframe": timeframe, "strategy": strategy,
        "candles": len(book.primary), "fixed_diagnostic_start_index": start,
        "fixed_first_decision_close_utc": book.primary[start].closed_at,
        "last_candle_close_utc": book.primary[-1].closed_at,
        "fixed_arms": fixed, "walk_forward_folds": folds,
        "cash_net_per_separate_account": 0.,
        "risk": asdict(ResearchRisk()), "pristine_holdout": False,
        "profitability_proven": False, "execution_authorized": False,
    }

"""Three predeclared entry/exit hypotheses, isolated from services and S3.

Confirmed proposals originate one closed candle earlier. Stops and holding
limits stay unchanged; target changes are estimates of net R/R, not guarantees.
"""
from dataclasses import asdict, replace
from datetime import timedelta
from math import isfinite

from app.strategy.gate_classical_s2 import MIN_HISTORY, TIMEFRAME_SECONDS
from app.strategy.gate_s2_replay import replay_segment, ResearchRisk
from app.strategy.gate_cost_viability_research import (
    DecisionBook, BASE_FRICTION, STRESS_FRICTION, estimated_round_trip_cost, metrics,
)

HYPOTHESES = ("CONFIRM_CLOSE", "NET_RR2", "CONFIRM_CLOSE_NET_RR2")


def adjust_proposal(plan, signal_bar, current_bar, cutoff, variant):
    if variant not in HYPOTHESES:
        raise ValueError("Unregistered entry/exit hypothesis")
    if (plan.execution_authorized is not False or plan.predictive_edge_proven is not False
            or cutoff != current_bar.closed_at+timedelta(microseconds=1)
            or (signal_bar.symbol, signal_bar.timeframe) != (plan.symbol, plan.timeframe)
            or (current_bar.symbol, current_bar.timeframe) != (plan.symbol, plan.timeframe)):
        raise ValueError("Unsafe proposal or mismatched decision clock")
    if plan.action == "WAIT":
        return replace(plan, cutoff=cutoff)
    # This validates the original version, stop, target and positive finite price.
    cost = estimated_round_trip_cost(plan, current_bar.close)
    if variant.startswith("CONFIRM"):
        if ((current_bar.closed_at-signal_bar.closed_at).total_seconds()
                != TIMEFRAME_SECONDS[plan.timeframe]
                or plan.cutoff != signal_bar.closed_at+timedelta(microseconds=1)):
            raise ValueError("Confirmation requires exactly the previous closed bar")
        long = plan.action == "LONG"
        confirmed = (current_bar.close > signal_bar.close if long
                     else current_bar.close < signal_bar.close)
        invalidated = (current_bar.low <= signal_bar.close-plan.stop_distance if long
                       else current_bar.high >= signal_bar.close+plan.stop_distance)
        if not confirmed or invalidated:
            return replace(plan, cutoff=cutoff, action="WAIT", stop_distance=None,
                           target_distance=None, reasoning="ENTRY_CONFIRMATION_REJECTED_V1")
    elif plan.cutoff != cutoff or signal_bar != current_bar:
        raise ValueError("Target-only hypothesis must use the current proposal")
    target = plan.target_distance
    if "NET_RR2" in variant:
        # Estimated net reward T-C >= 2*(S+C) -> T >= 2*S+3*C.
        # Gaps, realized duration and fills can defeat this estimate.
        target = max(target, 2*plan.stop_distance+3*cost)
        if not isfinite(target):
            raise ValueError("Nonfinite research target")
    return replace(plan, cutoff=cutoff, target_distance=target,
                   reasoning="ENTRY_EXIT_RESEARCH_V1:"+variant)


class EntryExitBook:
    def __init__(self, groups, timeframe, *, earliest_origin_close=None):
        self.controls = DecisionBook(groups[timeframe],
            {tf: groups[tf] for tf in ("1h", "4h")} if timeframe == "15m" else None)
        self.primary = self.controls.primary
        self.available = ("BASELINE",)+(("S1",) if timeframe == "15m" else ())+HYPOTHESES
        self.earliest_origin_close = earliest_origin_close

    def evaluate(self, strategy, prefix, cutoff, variant):
        if variant not in self.available:
            raise ValueError("Unregistered variant")
        current = self.controls.evaluate(strategy, prefix, cutoff,
                                         variant if variant in ("BASELINE", "S1") else "BASELINE")
        if variant in ("BASELINE", "S1"):
            return current
        if variant.startswith("CONFIRM"):
            if len(prefix) <= MIN_HISTORY or (self.earliest_origin_close is not None
                    and prefix[-2].closed_at < self.earliest_origin_close):
                return replace(current, action="WAIT", stop_distance=None, target_distance=None)
            earlier = prefix[:-1]
            prior = self.controls.evaluate(strategy, earlier,
                earlier[-1].closed_at+timedelta(microseconds=1), "BASELINE")
            return adjust_proposal(prior, earlier[-1], prefix[-1], cutoff, variant)
        return adjust_proposal(current, prefix[-1], prefix[-1], cutoff, variant)

    def run(self, strategy, variant, start, end, friction=BASE_FRICTION):
        return replay_segment(strategy, self.primary[:end], start_index=start, end_index=end,
            segment="ENTRY_EXIT_RESEARCH_V1", friction=friction, risk=ResearchRisk(),
            plan_evaluator=lambda s, p, c: self.evaluate(s, p, c, variant))

    def compare(self, strategy, start, end, variants=None):
        output = {}
        for scenario, friction in (("base", BASE_FRICTION), ("stress", STRESS_FRICTION)):
            output[scenario] = {}
            for variant in variants or self.available:
                r = self.run(strategy, variant, start, end, friction)
                output[scenario][variant] = {
                    "metrics": metrics(r, friction),
                    "trades": [{"trade": asdict(t), "signal_origin_index":
                        t.decision_bar_index-(1 if variant.startswith("CONFIRM") else 0)}
                        for t in r.trades],
                }
        return output


def select(training):
    hurdle = max(0., *(training[v]["completed_trade_net"]
                        for v in ("BASELINE", "S1") if v in training))
    eligible = []
    for v in HYPOTHESES:
        m = training[v]
        pf = m["completed_trade_profit_factor"]
        if (m["completed_trades"] >= 5 and m["completed_trade_net"] > hurdle
                and ((pf is not None and pf > 1) or m["all_completed_trades_profitable"])):
            eligible.append(v)
    return min(eligible, key=lambda v: (-training[v]["completed_trade_net"], v)) if eligible else None


def training_metrics(book, strategy, end):
    return {v: metrics(book.run(strategy, v, MIN_HISTORY, end), BASE_FRICTION)
            for v in book.available}


def evidence(comparison, selected, bars):
    controls = [v for v in ("BASELINE", "S1") if v in comparison["base"]]
    sufficient = bool(selected and bars >= 96 and all(
        comparison[s][v]["metrics"]["completed_trades"] >= 30
        for s in ("base", "stress") for v in [*controls, selected]))
    beats = bool(selected and all(
        comparison[s][selected]["metrics"]["completed_trade_net"] > max(
            0., *(comparison[s][v]["metrics"]["completed_trade_net"] for v in controls))
        for s in ("base", "stress")))
    return {"sufficient_completed_trades_each_arm": sufficient,
            "beats_cash_and_controls_base_and_stress": beats,
            "descriptive_screen_positive": sufficient and beats,
            "profitability_proven": False, "execution_authorized": False}


def historical_case(groups, strategy, timeframe):
    book = EntryExitBook(groups, timeframe)
    fixed = book.compare(strategy, 480, len(book.primary))
    folds = []
    for end_train in (400, 520, 640):
        training = training_metrics(book, strategy, end_train)
        chosen = select(training)
        names = [v for v in ("BASELINE", "S1") if v in book.available]
        comparison = book.compare(strategy, end_train, end_train+120,
                                  names+([chosen] if chosen else []))
        folds.append({"train_end_exclusive": end_train, "selected": chosen,
                      "training": training, "following_window": comparison,
                      "evidence": evidence(comparison, chosen, 120)})
    full_training = training_metrics(book, strategy, len(book.primary))
    return {"symbol": book.primary[0].symbol, "timeframe": timeframe, "strategy": strategy,
            "fixed_reused_history": fixed, "chronological_folds": folds,
            "full_old_history_training": full_training, "holdout_selected": select(full_training),
            "pristine_holdout": False, "profitability_proven": False}


def holdout_case(groups, old_case, start_utc):
    tf, strategy = old_case["timeframe"], old_case["strategy"]
    first_complete_close = start_utc+timedelta(seconds=TIMEFRAME_SECONDS[tf])
    book = EntryExitBook(groups, tf, earliest_origin_close=first_complete_close)
    if any(b.closed_at > b.received_at for rows in groups.values() for b in rows):
        raise ValueError("Invalid batch receipts")
    # Refuse a late sliding snapshot that has lost the start of the held-out window.
    if book.primary[MIN_HISTORY].closed_at > first_complete_close:
        raise ValueError("Snapshot lost holdout start or pre-freeze warmup")
    indexes = [i for i, b in enumerate(book.primary) if b.closed_at >= first_complete_close]
    selected = old_case["holdout_selected"]
    if selected is not None and selected not in HYPOTHESES:
        raise ValueError("Unknown frozen selection")
    if not indexes or len(book.primary)-indexes[0] < 2:
        return {"status": "WAITING_FOR_POST_FREEZE_CANDLES", "bars": 0,
                "selected": selected, "profitability_proven": False}
    start = indexes[0]
    names = [v for v in ("BASELINE", "S1") if v in book.available]
    comparison = book.compare(strategy, start, len(book.primary), names+([selected] if selected else []))
    ev = evidence(comparison, selected, len(indexes))
    return {"status": "DESCRIPTIVE_SCREEN_ONLY" if ev["sufficient_completed_trades_each_arm"]
            else "INSUFFICIENT_POST_FREEZE_EVIDENCE", "bars": len(indexes),
            "selected": selected, "selection_refit_on_holdout": False,
            "comparison": comparison, "evidence": ev,
            "batch_replay_not_observed_execution": True}

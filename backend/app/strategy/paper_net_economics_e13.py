"""E1.3 research-only net reward/risk scenario analysis of existing PAPER orders.

No execution, permission change, calibration or strategy promotion is performed.
Published execution assumptions below are *illustrative*, not verified as the
runtime parameters of the trades or evidence of an executable market fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median


@dataclass(frozen=True)
class CostScenario:
    name: str
    taker_fee_bps_per_side: float = 8.0
    half_spread_bps_at_exit: float = 1.0
    base_slippage_bps_at_exit: float = 2.0
    volume_impact_bps_at_exit: float = 0.0

    def __post_init__(self):
        values = (
            self.taker_fee_bps_per_side,
            self.half_spread_bps_at_exit,
            self.base_slippage_bps_at_exit,
            self.volume_impact_bps_at_exit,
        )
        if not all(isfinite(x) and x >= 0 for x in values):
            raise ValueError("Cost scenarios need nonnegative finite bps")

    @property
    def exit_adverse_bps(self) -> float:
        return (
            self.half_spread_bps_at_exit
            + self.base_slippage_bps_at_exit
            + self.volume_impact_bps_at_exit
        )


# Values reflect BacktestExecutionAssumptions defaults (8 bps taker, 1 bps
# half spread, 2 bps base slippage, up to 10 bps volume impact). The lower
# scenario assumes zero impact; the stress scenario assumes the maximum impact
# parameter, NOT an estimate based on volume absent from the E1 snapshot.
BASE_COST = CostScenario(name="BASE_NO_VOLUME_IMPACT")
STRESS_COST = CostScenario(
    name="STRESS_MAX_VOLUME_IMPACT", volume_impact_bps_at_exit=10.0,
)
SCENARIOS = (BASE_COST, STRESS_COST)


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def planned_net_economics(order, scenario: CostScenario) -> dict:
    """From observed PAPER entry fill, simulate two possible future exits.

    At the reference stop and target, apply adverse execution *once* to exit.
    Entry is already a fill price: do not apply spread/slippage to it again.
    Entry/exit fees are HYPOTHETICAL costs per the scenario, not a subtraction
    from recorded net P&L. No future candle, spread or funding is available.
    """
    identifier = str(order.id)
    result: dict = {
        "order_id": identifier, "symbol": str(order.symbol),
        "strategy": str(order.strategy), "side": str(order.side),
        "status": "INSUFFICIENT_EVIDENCE",
    }
    if (
        order.side not in {"BUY", "SELL"}
        or order.stop_loss is None or order.take_profit is None
        or order.entry_price is None or order.position_size is None
    ):
        result["reason"] = "MISSING_OR_INVALID_SIDE_STOP_TARGET"
        return result
    entry = float(order.entry_price)
    stop = float(order.stop_loss)
    target = float(order.take_profit)
    notional = float(order.position_size)
    if (
        not all(isfinite(x) for x in (entry, stop, target, notional))
        or entry <= 0 or stop <= 0 or target <= 0 or notional <= 0
    ):
        result["reason"] = "INVALID_NOTIONAL_OR_PRICE"
        return result
    sign = 1 if order.side == "BUY" else -1
    distance = sign * (entry - stop)
    reward_distance = sign * (target - entry)
    if distance <= 0 or reward_distance <= 0:
        result["reason"] = "INVALID_STOP_TARGET_SIDE"
        return result
    qty = notional / entry
    exit_multiplier = 1.0 - sign * scenario.exit_adverse_bps / 10_000.0
    fee_rate = scenario.taker_fee_bps_per_side / 10_000.0
    stop_exit = stop * exit_multiplier
    target_exit = target * exit_multiplier
    # For long positions the exit is a sell, and for short it is a buy.
    # sign BUY=+1 worsens exit downward; SELL=-1 worsens upward.
    entry_fee = notional * fee_rate

    def net_at_exit(fill: float) -> float:
        exit_fee = qty * fill * fee_rate
        gross = sign * (fill - entry) * qty
        return gross - entry_fee - exit_fee

    net_stop = net_at_exit(stop_exit)
    net_target = net_at_exit(target_exit)
    loss = max(0.0, -net_stop)
    reward = net_target
    rr = reward / loss if loss > 0 and reward > 0 else None
    break_even = loss / (loss + reward) if loss > 0 and reward > 0 else None
    planned_gross_risk = qty * distance
    result.update({
        "status": "EVALUATED",
        "gross_planned_risk": _round(planned_gross_risk),
        "gross_planned_reward_risk": _round(reward_distance / distance),
        "estimated_stop_exit": _round(stop_exit),
        "estimated_target_exit": _round(target_exit),
        "hypothetical_net_at_stop": _round(net_stop),
        "hypothetical_net_at_target": _round(net_target),
        "hypothetical_loss_if_stopped": _round(loss),
        "hypothetical_profit_if_target_hit": _round(reward),
        "hypothetical_net_reward_risk": _round(rr),
        "hypothetical_break_even_win_rate": _round(break_even),
        "estimated_round_trip_fees_at_stop": _round(entry_fee + qty * stop_exit * fee_rate),
        "exit_adverse_bps": scenario.exit_adverse_bps,
    })
    return result


def analyze_cost_diagnostics(
    orders, *, minimum_net_rr: float = 1.5,
    scenarios: tuple[CostScenario, ...] = SCENARIOS,
) -> dict:
    """Calculate hypothetical screens; NEVER authorize/reject real orders."""
    if not isfinite(minimum_net_rr) or minimum_net_rr <= 0:
        raise ValueError("minimum_net_rr must be finite and positive")
    ids = [o.id for o in orders]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate orders would double count screening")
    grouped = []
    for scenario in scenarios:
        findings = [planned_net_economics(o, scenario) for o in orders]
        evaluable = [x for x in findings if x["status"] == "EVALUATED"]
        ratios = [x["hypothetical_net_reward_risk"] for x in evaluable
                  if x["hypothetical_net_reward_risk"] is not None]
        failed = [x for x in evaluable
                  if x["hypothetical_net_reward_risk"] is None
                  or x["hypothetical_net_reward_risk"] < minimum_net_rr]
        grouped.append({
            "scenario": scenario.name,
            "assumptions": {
                "taker_fee_bps_per_side": scenario.taker_fee_bps_per_side,
                "exit_adverse_bps": scenario.exit_adverse_bps,
                "volume_impact_bps_at_exit": scenario.volume_impact_bps_at_exit,
            },
            "orders_examined": len(orders),
            "evaluable": len(evaluable),
            "insufficient_evidence": len(findings) - len(evaluable),
            "hypothetical_fails_minimum_net_rr": len(failed),
            "hypothetical_meets_minimum_net_rr": len(evaluable) - len(failed),
            "hypothetical_net_rr_median": _round(median(ratios)) if ratios else None,
            "lowest_five_by_net_rr": sorted(
                evaluable,
                key=lambda x: (
                    x["hypothetical_net_reward_risk"]
                    if x["hypothetical_net_reward_risk"] is not None else -1,
                    x["order_id"],
                ),
            )[:5],
            "per_order_results": findings,
        })
    return {
        "mode": "SHADOW_RESEARCH_ONLY_NO_EXECUTION",
        "assumptions_verified_against_staging_runtime": False,
        "minimum_net_rr_for_illustrative_screen": minimum_net_rr,
        "scenarios": grouped,
        "caveats": [
            "Default simulation costs, not authenticated historical venue costs.",
            "No point-in-time post-decision quote, spread, funding, gap or candle liquidity.",
            "Existing entry is already a PAPER fill; do not subtract adverse entry costs twice.",
            "Hypothetical exits do not replace realized P&L, actual fees or risk checks.",
            "No causal profitability gain or actionable OOS edge is inferred by screening.",
            "Historical sample is bounded; archive reconciliation remains outstanding.",
        ],
    }

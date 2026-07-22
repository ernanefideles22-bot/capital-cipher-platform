"""Pre-registered, multiplicity-adjusted walk-forward research gates."""

from __future__ import annotations

import math
from typing import Literal

from app.schemas.backtest import (
    WalkForwardAcceptanceCriteria,
    WalkForwardAggregate,
    WalkForwardGateResult,
    WalkForwardResearchPlan,
)


def _one_sided_sign_test_p_value(
    profitable_folds: int,
    folds: int,
) -> float:
    """Exact P(X >= profitable_folds) for X ~ Binomial(folds, 0.5)."""

    if folds <= 0:
        return 1.0
    numerator = sum(
        math.comb(folds, successes)
        for successes in range(profitable_folds, folds + 1)
    )
    return numerator / (2**folds)


def evaluate_walk_forward_gate(
    *,
    phase: Literal["VALIDATION", "TEST"],
    aggregate: WalkForwardAggregate,
    criteria: WalkForwardAcceptanceCriteria,
    research_plan: WalkForwardResearchPlan,
) -> WalkForwardGateResult:
    raw_p_value = _one_sided_sign_test_p_value(
        aggregate.profitable_folds,
        aggregate.folds,
    )
    adjusted_p_value = min(
        1.0,
        raw_p_value * research_plan.candidate_budget,
    )
    reasons: list[str] = []
    if aggregate.folds < criteria.minimum_folds:
        reasons.append(
            f"folds {aggregate.folds} below {criteria.minimum_folds}"
        )
    if aggregate.total_trades < criteria.minimum_trades:
        reasons.append(
            "trades "
            f"{aggregate.total_trades} below {criteria.minimum_trades}"
        )
    if (
        aggregate.profitable_fold_ratio
        < criteria.minimum_profitable_fold_ratio
    ):
        reasons.append(
            "profitable_fold_ratio "
            f"{aggregate.profitable_fold_ratio} below "
            f"{criteria.minimum_profitable_fold_ratio}"
        )
    if (
        aggregate.median_net_pnl_percent
        < criteria.minimum_median_net_pnl_percent
    ):
        reasons.append(
            "median_net_pnl_percent "
            f"{aggregate.median_net_pnl_percent} below "
            f"{criteria.minimum_median_net_pnl_percent}"
        )
    if aggregate.mean_expectancy < criteria.minimum_mean_expectancy:
        reasons.append(
            f"mean_expectancy {aggregate.mean_expectancy} below "
            f"{criteria.minimum_mean_expectancy}"
        )
    if (
        aggregate.worst_max_drawdown
        > criteria.maximum_worst_drawdown_percent
    ):
        reasons.append(
            f"worst_max_drawdown {aggregate.worst_max_drawdown} exceeds "
            f"{criteria.maximum_worst_drawdown_percent}"
        )
    if (
        criteria.require_zero_liquidations
        and aggregate.total_liquidations > 0
    ):
        reasons.append(
            f"liquidations {aggregate.total_liquidations} must be zero"
        )
    if adjusted_p_value > research_plan.familywise_alpha:
        reasons.append(
            "Bonferroni-adjusted sign-test p-value "
            f"{adjusted_p_value:.6f} exceeds "
            f"{research_plan.familywise_alpha}"
        )

    return WalkForwardGateResult(
        phase=phase,
        passed=not reasons,
        reasons=reasons,
        raw_sign_test_p_value=round(raw_p_value, 8),
        adjusted_p_value=round(adjusted_p_value, 8),
        familywise_alpha=research_plan.familywise_alpha,
        observed={
            "folds": aggregate.folds,
            "total_trades": aggregate.total_trades,
            "profitable_fold_ratio": aggregate.profitable_fold_ratio,
            "median_net_pnl_percent": (
                aggregate.median_net_pnl_percent
            ),
            "mean_expectancy": aggregate.mean_expectancy,
            "worst_max_drawdown": aggregate.worst_max_drawdown,
            "total_liquidations": aggregate.total_liquidations,
            "candidate_budget": research_plan.candidate_budget,
        },
    )

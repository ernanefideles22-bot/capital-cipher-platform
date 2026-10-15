"""Governed read-only candidacy for specialist shadow tests.

This module does not alter consensus weights, risk decisions, OMS authority, or
execution. It only converts existing immutable agent scorecards into an
explicit eligibility read model for the next observational phase.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.schemas.specialist_evaluation import AgentScorecard

RANDOM_BASELINE_BRIER = 0.25
MIN_DIRECTIONAL_ACCURACY = 0.50
MIN_MARGINAL_CONTRIBUTION = 0.0


class SpecialistCandidacyService:
    """Classify evaluated agents for regime-specific SHADOW testing only."""

    @staticmethod
    def classify(scorecards: Iterable[AgentScorecard]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for card in scorecards:
            sample_ready = card.status == "EVALUATED" and card.sample_count >= card.minimum_samples
            accuracy_ready = card.accuracy is not None and card.accuracy > MIN_DIRECTIONAL_ACCURACY
            calibration_ready = (
                card.mean_brier_loss is not None
                and card.mean_brier_loss < RANDOM_BASELINE_BRIER
            )
            contribution_ready = (
                card.mean_marginal_contribution is not None
                and card.mean_marginal_contribution > MIN_MARGINAL_CONTRIBUTION
            )
            eligible = all(
                (
                    sample_ready,
                    accuracy_ready,
                    calibration_ready,
                    contribution_ready,
                )
            )
            status = (
                "CANDIDATE_FOR_REGIME_SHADOW"
                if eligible
                else "OBSERVING"
                if not sample_ready
                else "EVALUATED_NOT_QUALIFIED"
            )
            rows.append(
                {
                    "agent_name": card.agent_name,
                    "agent_version": card.agent_version,
                    "status": status,
                    "eligible_for_regime_shadow_test": eligible,
                    "decision_authority": False,
                    "automatic_weight_adjustment": False,
                    "sample_count": card.sample_count,
                    "minimum_samples": card.minimum_samples,
                    "accuracy": card.accuracy,
                    "mean_brier_loss": card.mean_brier_loss,
                    "mean_marginal_contribution": card.mean_marginal_contribution,
                    "criteria": {
                        "minimum_sample_reached": sample_ready,
                        "accuracy_above_50_percent": accuracy_ready,
                        "brier_below_random_baseline": calibration_ready,
                        "positive_marginal_contribution": contribution_ready,
                    },
                }
            )

        def sort_key(row: dict[str, Any]) -> tuple:
            marginal = row["mean_marginal_contribution"]
            accuracy = row["accuracy"]
            brier = row["mean_brier_loss"]
            return (
                0 if row["eligible_for_regime_shadow_test"] else 1,
                -(marginal if marginal is not None else -1.0),
                -(accuracy if accuracy is not None else -1.0),
                brier if brier is not None else 2.0,
                -row["sample_count"],
                row["agent_name"],
            )

        return sorted(rows, key=sort_key)

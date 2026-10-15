from app.agents.candidacy import SpecialistCandidacyService
from app.schemas.specialist_evaluation import AgentScorecard


def card(
    *,
    name: str,
    sample_count: int,
    accuracy: float | None,
    brier: float | None,
    marginal: float | None,
) -> AgentScorecard:
    status = "EVALUATED" if sample_count >= 30 else "INSUFFICIENT_SAMPLE"
    return AgentScorecard(
        agent_name=name,
        agent_version="1.0.0",
        sample_count=sample_count,
        directional_sample_count=sample_count,
        accuracy=accuracy,
        mean_brier_loss=brier,
        mean_marginal_contribution=marginal,
        status=status,
        minimum_samples=30,
    )


def test_only_strong_evaluated_agent_becomes_regime_shadow_candidate() -> None:
    rows = SpecialistCandidacyService.classify(
        [
            card(
                name="QualifiedAgent",
                sample_count=80,
                accuracy=0.61,
                brier=0.19,
                marginal=0.03,
            ),
            card(
                name="InsufficientAgent",
                sample_count=12,
                accuracy=0.75,
                brier=0.10,
                marginal=0.08,
            ),
            card(
                name="NoContributionAgent",
                sample_count=80,
                accuracy=0.62,
                brier=0.18,
                marginal=0.0,
            ),
        ]
    )

    assert rows[0]["agent_name"] == "QualifiedAgent"
    assert rows[0]["status"] == "CANDIDATE_FOR_REGIME_SHADOW"
    assert rows[0]["eligible_for_regime_shadow_test"] is True
    assert rows[0]["decision_authority"] is False
    assert rows[0]["automatic_weight_adjustment"] is False

    by_name = {row["agent_name"]: row for row in rows}
    assert by_name["InsufficientAgent"]["status"] == "OBSERVING"
    assert by_name["NoContributionAgent"]["status"] == "EVALUATED_NOT_QUALIFIED"


def test_threshold_edges_do_not_qualify() -> None:
    rows = SpecialistCandidacyService.classify(
        [
            card(
                name="AccuracyEdgeAgent",
                sample_count=30,
                accuracy=0.50,
                brier=0.20,
                marginal=0.01,
            ),
            card(
                name="BrierEdgeAgent",
                sample_count=30,
                accuracy=0.60,
                brier=0.25,
                marginal=0.01,
            ),
        ]
    )

    assert all(row["eligible_for_regime_shadow_test"] is False for row in rows)

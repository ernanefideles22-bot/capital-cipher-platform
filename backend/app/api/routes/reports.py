"""Performance reports (docs/07 Fase 2, docs/27)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/reports")


@router.get("/performance")
async def performance_report(
    context: AppContext = Depends(get_context),
    by: str = Query(default="symbol", pattern="^(symbol|timeframe)$"),
) -> dict:
    engine = context.paper_engine
    return success_response(
        {
            "overall": engine.performance().model_dump(mode="json"),
            "breakdown_by": by,
            "breakdown": [p.model_dump(mode="json") for p in engine.performance_by(by)],
            "equity_curve": [p.model_dump(mode="json") for p in engine.equity_curve],
        }
    )


@router.get("/agents/ranking")
async def agent_ranking(context: AppContext = Depends(get_context)) -> dict:
    from app.orchestrator.ranking import AgentRankingService

    service = AgentRankingService(context.orchestrator, context.paper_engine)
    return success_response({"ranking": service.report()})


@router.get("/agents/specialists")
async def specialist_evaluation_report(
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.agent_evaluation_service
    if service is None:
        return error_response(
            "AGENT_EVALUATION_UNAVAILABLE",
            "Agent evaluation service is not configured",
        )
    cards = await service.scorecards()
    return success_response(
        {
            "scorecards": [card.model_dump(mode="json") for card in cards],
            "decision_authority": False,
            "automatic_weight_adjustment": False,
        }
    )


@router.get("/agents/specialist-candidates")
async def specialist_candidate_report(
    context: AppContext = Depends(get_context),
) -> dict:
    from app.agents.candidacy import SpecialistCandidacyService

    service = context.agent_evaluation_service
    if service is None:
        return error_response(
            "AGENT_EVALUATION_UNAVAILABLE",
            "Agent evaluation service is not configured",
        )
    cards = await service.scorecards()
    candidates = SpecialistCandidacyService.classify(cards)
    return success_response(
        {
            "candidates": candidates,
            "decision_authority": False,
            "automatic_weight_adjustment": False,
            "next_phase": "REGIME_SHADOW_OBSERVATION",
        }
    )


@router.get("/agents/regime-shadow")
async def specialist_regime_shadow_report(
    context: AppContext = Depends(get_context),
) -> dict:
    """Performance by the exact historical regime, with look-ahead protection."""
    from app.agents.candidacy import SpecialistCandidacyService
    from app.agents.regime_evaluation import RegimeShadowEvaluationService

    service = context.agent_evaluation_service
    if service is None:
        return error_response(
            "AGENT_EVALUATION_UNAVAILABLE",
            "Agent evaluation service is not configured",
        )
    cards = await service.scorecards()
    candidacy = SpecialistCandidacyService.classify(cards)
    candidate_names = {
        row["agent_name"]
        for row in candidacy
        if row["eligible_for_regime_shadow_test"]
    }
    evaluator = RegimeShadowEvaluationService(service, context.candle_store)
    report = await evaluator.report(candidate_names=candidate_names)
    return success_response(
        {
            **report,
            "candidate_count": len(candidate_names),
            "candidate_names": sorted(candidate_names),
        }
    )

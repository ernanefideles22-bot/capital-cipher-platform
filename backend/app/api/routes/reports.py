"""Performance reports (docs/07 Fase 2, docs/27)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import success_response

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

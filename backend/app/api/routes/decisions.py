"""Decision endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/decisions")


@router.get("")
async def list_decisions(context: AppContext = Depends(get_context)) -> dict:
    decisions = [d.model_dump(mode="json") for d in list(context.orchestrator.recent_decisions)[-50:]]
    return success_response({"decisions": list(reversed(decisions))})


@router.get("/{decision_id}")
async def get_decision(decision_id: str, context: AppContext = Depends(get_context)) -> dict:
    for decision in context.orchestrator.recent_decisions:
        if decision.decision_id == decision_id:
            return success_response({"decision": decision.model_dump(mode="json")})
    return error_response("NOT_FOUND", f"Decision {decision_id} not found")

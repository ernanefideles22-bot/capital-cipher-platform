"""Decision endpoints (docs/13)."""

from __future__ import annotations

from datetime import timezone

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response
from app.schemas.decisions import Decision

router = APIRouter(prefix="/decisions")


def durable_decision(row) -> dict:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return Decision(
        decision_id=row.id, correlation_id=row.correlation_id,
        symbol=row.symbol, timeframe=row.timeframe,
        candidate_action=row.candidate_action, confidence=row.confidence,
        strategy=row.strategy or "UNSPECIFIED", warnings=row.warnings or [],
        reason=row.reason or "", agent_summary=row.agent_summary,
        risk_status=row.risk_status, created_at=created_at,
    ).model_dump(mode="json")


@router.get("")
async def list_decisions(context: AppContext = Depends(get_context)) -> dict:
    if context.repository is not None:
        rows = await context.repository.get_decisions(limit=50)
        return success_response({"decisions": [durable_decision(row) for row in rows]})
    decisions = [d.model_dump(mode="json") for d in list(context.orchestrator.recent_decisions)[-50:]]
    return success_response({"decisions": list(reversed(decisions))})


@router.get("/{decision_id}")
async def get_decision(decision_id: str, context: AppContext = Depends(get_context)) -> dict:
    if context.repository is not None:
        row = await context.repository.get_decision(decision_id)
        if row is not None:
            return success_response({"decision": durable_decision(row)})
        return error_response("NOT_FOUND", f"Decision {decision_id} not found")
    for decision in context.orchestrator.recent_decisions:
        if decision.decision_id == decision_id:
            return success_response({"decision": decision.model_dump(mode="json")})
    return error_response("NOT_FOUND", f"Decision {decision_id} not found")

"""Strategy endpoints (docs/26). Enable/disable requires authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/strategies")


class ToggleRequest(BaseModel):
    enabled: bool


@router.get("")
async def list_strategies(context: AppContext = Depends(get_context)) -> dict:
    return success_response(
        {
            "strategies": [
                s.model_dump(mode="json")
                for s in context.orchestrator.strategy_engine.list()
            ]
        }
    )


@router.post("/{versioned_id}/toggle", dependencies=[AdminRequired])
async def toggle_strategy(
    versioned_id: str, body: ToggleRequest, context: AppContext = Depends(get_context)
) -> dict:
    try:
        strategy = context.orchestrator.strategy_engine.set_enabled(versioned_id, body.enabled)
    except KeyError:
        return error_response("NOT_FOUND", f"Strategy {versioned_id} not found")
    await context.audit_service.record(
        correlation_id="00000000-0000-0000-0000-000000000000",
        audit_type="STRATEGY_TOGGLED",
        entity_type="strategy",
        entity_id=versioned_id,
        payload={"enabled": body.enabled},
    )
    return success_response({"strategy": strategy.model_dump(mode="json")})

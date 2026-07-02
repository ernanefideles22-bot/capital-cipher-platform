"""Risk endpoints (docs/13). Kill switch requires authentication (docs/16)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import success_response

router = APIRouter(prefix="/risk")


class KillSwitchRequest(BaseModel):
    reason: str


@router.get("/status")
async def risk_status(context: AppContext = Depends(get_context)) -> dict:
    state = context.risk_manager.state
    return success_response(
        {
            "daily_pnl_percent": round(state.daily_pnl_percent, 4),
            "consecutive_losses": state.consecutive_losses,
            "open_positions": state.open_positions,
            "blocked_operations": state.blocked_operations,
            "kill_switch_active": context.state_machine.kill_switch_active,
            "kill_switch_reason": context.state_machine.kill_switch_reason,
        }
    )


@router.get("/limits")
async def risk_limits(context: AppContext = Depends(get_context)) -> dict:
    return success_response(context.risk_manager.limits.model_dump(mode="json"))


@router.post("/kill-switch", dependencies=[AdminRequired])
async def kill_switch(
    body: KillSwitchRequest, context: AppContext = Depends(get_context)
) -> dict:
    await context.state_machine.trigger_kill_switch(reason=body.reason, actor="api")
    await context.audit_service.record(
        correlation_id="00000000-0000-0000-0000-000000000000",
        audit_type="KILL_SWITCH_TRIGGERED",
        entity_type="system",
        payload={"reason": body.reason, "actor": "api"},
    )
    return success_response(
        {
            "kill_switch_active": True,
            "system_state": context.state_machine.state.value,
            "reason": body.reason,
        }
    )

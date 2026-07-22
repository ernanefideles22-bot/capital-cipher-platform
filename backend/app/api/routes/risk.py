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
            "control_revision": context.risk_manager.control_state.revision,
            "durable_control_active": context.risk_manager.control_state.active,
            "portfolio": context.risk_manager.portfolio_status(
                balance=context.paper_engine.balance
            ),
        }
    )


@router.get("/limits")
async def risk_limits(context: AppContext = Depends(get_context)) -> dict:
    return success_response(context.risk_manager.limits.model_dump(mode="json"))


@router.post("/kill-switch", dependencies=[AdminRequired])
async def kill_switch(
    body: KillSwitchRequest, context: AppContext = Depends(get_context)
) -> dict:
    await context.risk_manager.trigger_kill_switch(
        reason=body.reason,
        actor="api",
    )
    return success_response(
        {
            "kill_switch_active": True,
            "system_state": context.state_machine.state.value,
            "reason": body.reason,
        }
    )


@router.post("/kill-switch/reset", dependencies=[AdminRequired])
async def reset_kill_switch(
    body: KillSwitchRequest,
    context: AppContext = Depends(get_context),
) -> dict:
    await context.risk_manager.reset_kill_switch(
        reason=body.reason,
        actor="api",
    )
    return success_response(
        {
            "kill_switch_active": False,
            "system_state": context.state_machine.state.value,
            "reason": body.reason,
            "control_revision": context.risk_manager.control_state.revision,
        }
    )

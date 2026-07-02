"""System status endpoint (docs/13 GET /status)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import success_response

router = APIRouter()


@router.get("/status")
async def system_status(context: AppContext = Depends(get_context)) -> dict:
    return success_response(
        {
            "mode": context.state_machine.state.value,
            "kill_switch_active": context.state_machine.kill_switch_active,
            "market_data": "CONNECTED" if context.market_connected else "DISCONNECTED",
            "orchestrator": "RUNNING" if context.state_machine.can_operate() else "IDLE",
            "risk": "ACTIVE",
            "database": "CONNECTED" if context.repository is not None else "IN_MEMORY",
        }
    )

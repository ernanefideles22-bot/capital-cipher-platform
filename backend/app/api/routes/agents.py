"""Agent endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/agents")


@router.get("")
async def list_agents(context: AppContext = Depends(get_context)) -> dict:
    return success_response(
        {
            "agents": [
                agent.registration().model_dump(mode="json")
                for agent in context.orchestrator.agents.values()
            ]
        }
    )


@router.get("/status")
async def agents_status(context: AppContext = Depends(get_context)) -> dict:
    return success_response(
        {
            "agents": [
                agent.health().model_dump(mode="json")
                for agent in context.orchestrator.agents.values()
            ]
        }
    )


@router.get("/{agent_name}/last-output")
async def agent_last_output(
    agent_name: str, context: AppContext = Depends(get_context)
) -> dict:
    agent = context.orchestrator.agents.get(agent_name)
    if agent is None:
        return error_response("NOT_FOUND", f"Agent {agent_name} not found")
    output = agent.last_output.model_dump(mode="json") if agent.last_output else None
    return success_response({"agent": agent_name, "last_output": output})

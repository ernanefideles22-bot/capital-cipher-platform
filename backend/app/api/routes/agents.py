"""Agent endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response
from app.schemas.agents import AgentExecutionRequest

router = APIRouter(prefix="/agents")


@router.get("")
async def list_agents(context: AppContext = Depends(get_context)) -> dict:
    agents = (
        context.agent_registry.agents
        if context.agent_registry is not None
        else context.orchestrator.agents
    )
    return success_response(
        {
            "agents": [
                agent.registration().model_dump(mode="json")
                for agent in agents.values()
            ]
        }
    )


@router.get("/status")
async def agents_status(context: AppContext = Depends(get_context)) -> dict:
    agents = (
        context.agent_registry.agents
        if context.agent_registry is not None
        else context.orchestrator.agents
    )
    return success_response(
        {
            "agents": [
                agent.health().model_dump(mode="json")
                for agent in agents.values()
            ]
        }
    )


@router.post("/executions", dependencies=[AdminRequired])
async def enqueue_agent_execution(
    request: AgentExecutionRequest,
    context: AppContext = Depends(get_context),
) -> dict:
    if context.agent_runtime is None:
        return error_response(
            "AGENT_RUNTIME_UNAVAILABLE",
            "Agent runtime is not configured",
        )
    job = await context.agent_runtime.enqueue(request)
    return success_response({"execution": job.model_dump(mode="json")})


@router.get("/executions", dependencies=[AdminRequired])
async def list_agent_executions(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    if context.agent_runtime is None:
        return error_response(
            "AGENT_RUNTIME_UNAVAILABLE",
            "Agent runtime is not configured",
        )
    jobs = await context.agent_runtime.list_jobs(limit=limit)
    return success_response(
        {
            "executions": [
                job.model_dump(mode="json") for job in jobs
            ]
        }
    )


@router.get("/executions/{execution_id}", dependencies=[AdminRequired])
async def agent_execution_trace(
    execution_id: str,
    context: AppContext = Depends(get_context),
) -> dict:
    if context.agent_runtime is None:
        return error_response(
            "AGENT_RUNTIME_UNAVAILABLE",
            "Agent runtime is not configured",
        )
    trace = await context.agent_runtime.trace(execution_id)
    if trace is None:
        return error_response(
            "NOT_FOUND",
            f"Agent execution {execution_id} not found",
        )
    return success_response({"trace": trace.model_dump(mode="json")})


@router.get("/{agent_name}/last-output")
async def agent_last_output(
    agent_name: str, context: AppContext = Depends(get_context)
) -> dict:
    agents = (
        context.agent_registry.agents
        if context.agent_registry is not None
        else context.orchestrator.agents
    )
    agent = agents.get(agent_name)
    if agent is None:
        return error_response("NOT_FOUND", f"Agent {agent_name} not found")
    output = agent.last_output.model_dump(mode="json") if agent.last_output else None
    return success_response({"agent": agent_name, "last_output": output})

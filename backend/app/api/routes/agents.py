"""Agent endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response
from app.schemas.agents import AgentExecutionRequest
from app.schemas.specialist_evaluation import (
    SpecialistDomain,
    SpecialistEvidence,
)

router = APIRouter(prefix="/agents")


@router.post("/evidence", dependencies=[AdminRequired])
async def ingest_specialist_evidence(
    evidence: SpecialistEvidence,
    context: AppContext = Depends(get_context),
) -> dict:
    if context.specialist_evidence_service is None:
        return error_response(
            "SPECIALIST_EVIDENCE_UNAVAILABLE",
            "Specialist evidence service is not configured",
        )
    stored = await context.specialist_evidence_service.ingest(evidence)
    return success_response({"evidence": stored.model_dump(mode="json")})


@router.get("/evidence", dependencies=[AdminRequired])
async def list_specialist_evidence(
    domain: SpecialistDomain | None = None,
    metric_name: str | None = Query(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    ),
    scope: str | None = Query(
        default=None,
        pattern=r"^(GLOBAL|[A-Z0-9._-]{2,32})$",
    ),
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    if context.specialist_evidence_service is None:
        return error_response(
            "SPECIALIST_EVIDENCE_UNAVAILABLE",
            "Specialist evidence service is not configured",
        )
    items = await context.specialist_evidence_service.list(
        domain=domain,
        metric_name=metric_name,
        scope=scope,
        limit=limit,
    )
    return success_response(
        {"evidence": [item.model_dump(mode="json") for item in items]}
    )


@router.get("/evaluation/scorecards", dependencies=[AdminRequired])
async def agent_scorecards(
    context: AppContext = Depends(get_context),
) -> dict:
    if context.agent_evaluation_service is None:
        return error_response(
            "AGENT_EVALUATION_UNAVAILABLE",
            "Agent evaluation service is not configured",
        )
    cards = await context.agent_evaluation_service.scorecards()
    return success_response(
        {
            "scorecards": [card.model_dump(mode="json") for card in cards],
            "decision_authority": False,
        }
    )


@router.get("/evaluation/forecasts", dependencies=[AdminRequired])
async def agent_forecasts(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    if context.agent_evaluation_service is None:
        return error_response(
            "AGENT_EVALUATION_UNAVAILABLE",
            "Agent evaluation service is not configured",
        )
    forecasts = await context.agent_evaluation_service.forecasts(limit=limit)
    return success_response(
        {
            "forecasts": [
                forecast.model_dump(mode="json")
                for forecast in forecasts
            ],
            "decision_authority": False,
        }
    )


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

"""Health and status endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from app.api.context import AppContext
from app.api.deps import get_context

router = APIRouter()


@router.get("/health")
async def health(context: AppContext = Depends(get_context)) -> dict:
    return {
        "status": "ok",
        "service": context.settings.app_name,
        "version": context.settings.app_version,
    }


@router.get("/ready")
async def readiness(
    response: Response,
    context: AppContext = Depends(get_context),
) -> dict:
    """Deep, credential-free readiness for container orchestration.

    Shared staging is ready only when its durable dependencies, 300-agent
    PAPER cohort, market feed and execution boundary are all healthy.
    """

    settings = context.settings
    database_required = settings.app_env == "staging"
    if context.database is None:
        database_healthy = not database_required
    else:
        try:
            database_healthy = await context.database.healthcheck()
        except Exception:
            database_healthy = False

    if context.event_transport is None:
        broker_healthy = not settings.event_broker_required
    else:
        try:
            broker_healthy = await context.event_transport.healthcheck()
        except Exception:
            broker_healthy = False

    registrations = (
        context.agent_registry.registrations()
        if context.agent_registry is not None
        else []
    )
    cohort_healthy = (
        len(registrations) == 300
        and sum(item.decision_role == "PRIMARY" for item in registrations) == 3
        and sum(item.decision_role == "SHADOW" for item in registrations) == 297
        and all(item.execution_mode == "PAPER" for item in registrations)
    )
    checks = {
        "database": database_healthy,
        "broker": broker_healthy,
        "paper_execution_boundary": (
            settings.system_mode == "PAPER"
            and context.oms_service.target_environment.value == "PAPER"
        ),
        "agent_cohort": cohort_healthy,
        "operations_monitor": context.operations_service is not None,
        "market_data": (
            context.market_connected if settings.enable_market_data else True
        ),
        "kill_switch_clear": not context.state_machine.kill_switch_active,
        "system_state": context.state_machine.state.value == "PAPER",
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "checks": checks,
        "live_execution_available": False,
    }

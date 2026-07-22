"""Protected Month 9 portfolio and consensus governance APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response
from app.schemas.portfolio_consensus import (
    ConsensusExperiment,
    ConsensusExperimentEvent,
)

router = APIRouter(
    prefix="/governance",
    dependencies=[AdminRequired],
)


@router.post("/experiments")
async def register_consensus_experiment(
    experiment: ConsensusExperiment,
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.consensus_experiment_service
    if service is None:
        return error_response(
            "CONSENSUS_GOVERNANCE_UNAVAILABLE",
            "Consensus experiment service is not configured",
        )
    stored = await service.register(experiment)
    await context.audit_service.record(
        correlation_id=stored.experiment_id[:36],
        audit_type="CONSENSUS_EXPERIMENT_REGISTERED",
        entity_type="consensus_experiment",
        entity_id=stored.experiment_id,
        payload=stored.model_dump(mode="json"),
    )
    return success_response(
        {"experiment": stored.model_dump(mode="json")}
    )


@router.post("/experiments/{experiment_id}/events")
async def record_consensus_experiment_event(
    experiment_id: str,
    event: ConsensusExperimentEvent,
    context: AppContext = Depends(get_context),
) -> dict:
    if event.experiment_id != experiment_id:
        return error_response(
            "EXPERIMENT_ID_MISMATCH",
            "Path and lifecycle event experiment IDs differ",
        )
    service = context.consensus_experiment_service
    if service is None:
        return error_response(
            "CONSENSUS_GOVERNANCE_UNAVAILABLE",
            "Consensus experiment service is not configured",
        )
    stored = await service.record_event(event)
    await context.audit_service.record(
        correlation_id=stored.event_id[:36],
        audit_type="CONSENSUS_EXPERIMENT_EVENT",
        entity_type="consensus_experiment_event",
        entity_id=stored.event_id,
        payload=stored.model_dump(mode="json"),
    )
    return success_response({"event": stored.model_dump(mode="json")})


@router.get("/experiments")
async def list_consensus_experiments(
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.consensus_experiment_service
    if service is None:
        return error_response(
            "CONSENSUS_GOVERNANCE_UNAVAILABLE",
            "Consensus experiment service is not configured",
        )
    active = service.active()
    return success_response(
        {
            "active_experiment_id": active.experiment_id,
            "experiments": [
                item.model_dump(mode="json")
                for item in service.list_experiments()
            ],
            "events": [
                item.model_dump(mode="json")
                for item in service.list_events()
            ],
        }
    )


@router.get("/consensus")
async def list_weighted_consensus(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.weighted_consensus_service
    if service is None:
        return error_response(
            "WEIGHTED_CONSENSUS_UNAVAILABLE",
            "Weighted consensus service is not configured",
        )
    return success_response(
        {
            "snapshots": [
                item.model_dump(mode="json")
                for item in service.list(limit=limit)
            ]
        }
    )


@router.get("/drift")
async def list_drift_observations(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.drift_monitor
    if service is None:
        return error_response(
            "DRIFT_MONITOR_UNAVAILABLE",
            "Drift monitor is not configured",
        )
    return success_response(
        {
            "observations": [
                item.model_dump(mode="json")
                for item in service.list(limit=limit)
            ]
        }
    )


@router.get("/portfolio-proposals")
async def list_portfolio_proposals(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.portfolio_construction_service
    if service is None:
        return error_response(
            "PORTFOLIO_CONSTRUCTION_UNAVAILABLE",
            "Portfolio construction service is not configured",
        )
    return success_response(
        {
            "proposals": [
                item.model_dump(mode="json")
                for item in service.list(limit=limit)
            ],
            "order_authority": False,
        }
    )

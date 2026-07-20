"""Protected Month 10 operational control-plane APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response

router = APIRouter(
    prefix="/operations",
    dependencies=[AdminRequired],
)


def _service(context: AppContext):
    return context.operations_service


@router.get("/status")
async def operations_status(
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response(
            "OPERATIONS_UNAVAILABLE",
            "Operational control plane is not configured",
        )
    return success_response(service.status())


@router.get("/metrics")
async def operational_metrics(
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response(
            "OPERATIONS_UNAVAILABLE",
            "Operational control plane is not configured",
        )
    snapshot = await service.capture_snapshot(
        correlation_id="admin-observability-snapshot",
        persist=False,
    )
    return success_response(
        {"snapshot": snapshot.model_dump(mode="json")}
    )


@router.post("/slos/evaluate")
async def evaluate_slos(
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response(
            "OPERATIONS_UNAVAILABLE",
            "Operational control plane is not configured",
        )
    evaluations = await service.evaluate_slos(
        correlation_id="admin-slo-evaluation",
    )
    await context.audit_service.record(
        correlation_id="admin-slo-evaluation",
        audit_type="SLO_EVALUATED",
        entity_type="operations",
        entity_id=None,
        payload={
            "evaluation_ids": [
                item.evaluation_id for item in evaluations
            ],
            "order_authority": False,
        },
    )
    return success_response(
        {
            "evaluations": [
                item.model_dump(mode="json")
                for item in evaluations
            ]
        }
    )


@router.get("/slos")
async def list_slos(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response("OPERATIONS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "evaluations": [
                item.model_dump(mode="json")
                for item in service.slo_evaluations(limit=limit)
            ]
        }
    )


@router.get("/alerts")
async def list_alerts(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response("OPERATIONS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "active": [
                item.model_dump(mode="json")
                for item in service.active_alerts()
            ],
            "events": [
                item.model_dump(mode="json")
                for item in service.alert_events(limit=limit)
            ],
        }
    )


@router.get("/costs")
async def list_costs(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response("OPERATIONS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "budget": service.budget_status().model_dump(mode="json"),
            "records": [
                item.model_dump(mode="json")
                for item in service.cost_records(limit=limit)
            ],
            "primary_admission_always_allowed": True,
        }
    )


@router.get("/resilience-runs")
async def list_resilience_runs(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = _service(context)
    if service is None:
        return error_response("OPERATIONS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "runs": [
                item.model_dump(mode="json")
                for item in service.resilience_runs(limit=limit)
            ],
            "chaos_injection_api_available": False,
        }
    )

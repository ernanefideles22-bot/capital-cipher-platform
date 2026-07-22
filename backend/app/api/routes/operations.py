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


@router.get("/shadow-validation/reports")
async def list_shadow_validation_reports(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.shadow_validation_service
    if service is None:
        return error_response("SHADOW_VALIDATION_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "reports": [
                item.model_dump(mode="json")
                for item in service.reports(limit=limit)
            ],
            "campaign_start_api_available": False,
            "live_execution_available": False,
        }
    )


@router.get("/shadow-validation/checkpoints")
async def list_shadow_validation_checkpoints(
    campaign_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.shadow_validation_service
    if service is None:
        return error_response("SHADOW_VALIDATION_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "checkpoints": [
                item.model_dump(mode="json")
                for item in service.checkpoints(
                    campaign_id=campaign_id,
                    limit=limit,
                )
            ],
            "mutation_api_available": False,
        }
    )


@router.get("/release-readiness/evidence")
async def list_release_evidence(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.release_readiness_service
    if service is None:
        return error_response("RELEASE_READINESS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "evidence_bundles": [
                item.model_dump(mode="json")
                for item in service.evidence_bundles(limit=limit)
            ],
            "mutation_api_available": False,
            "live_execution_available": False,
        }
    )


@router.get("/release-readiness/attestations")
async def list_release_attestations(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.release_readiness_service
    if service is None:
        return error_response("RELEASE_READINESS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "attestations": [
                item.model_dump(mode="json")
                for item in service.attestations(limit=limit)
            ],
            "external_submission_api_available": False,
        }
    )


@router.get("/release-readiness/canary-drills")
async def list_testnet_canary_drills(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.release_readiness_service
    if service is None:
        return error_response("RELEASE_READINESS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "canary_drills": [
                item.model_dump(mode="json")
                for item in service.canary_drills(limit=limit)
            ],
            "remote_testnet_call_available": False,
            "real_funds_used": False,
        }
    )


@router.get("/release-readiness/gates")
async def list_release_gates(
    limit: int = Query(default=100, ge=1, le=1_000),
    context: AppContext = Depends(get_context),
) -> dict:
    service = context.release_readiness_service
    if service is None:
        return error_response("RELEASE_READINESS_UNAVAILABLE", "Unavailable")
    return success_response(
        {
            "gate_decisions": [
                item.model_dump(mode="json")
                for item in service.gate_decisions(limit=limit)
            ],
            "runtime_activation_api_available": False,
            "live_execution_available": False,
        }
    )

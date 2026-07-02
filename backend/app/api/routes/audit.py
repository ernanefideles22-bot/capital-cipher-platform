"""Audit endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import success_response

router = APIRouter(prefix="/audit")


@router.get("/events")
async def audit_events(
    context: AppContext = Depends(get_context),
    audit_type: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> dict:
    events = context.audit_service.query(audit_type=audit_type, limit=limit)
    return success_response({"events": events})


@router.get("/correlation/{correlation_id}")
async def audit_chain(correlation_id: str, context: AppContext = Depends(get_context)) -> dict:
    """Reconstruct the full decision chain for a correlation_id (docs/13)."""
    events = context.audit_service.query(correlation_id=correlation_id, limit=500)
    return success_response({"correlation_id": correlation_id, "chain": events})

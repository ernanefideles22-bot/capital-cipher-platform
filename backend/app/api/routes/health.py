"""Health and status endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

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

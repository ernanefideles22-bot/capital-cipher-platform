"""System status endpoint (docs/13 GET /status)."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import success_response

router = APIRouter()


@router.get("/status")
async def system_status(context: AppContext = Depends(get_context)) -> dict:
    evidence_source_revision = os.getenv("RELEASE_EVIDENCE_SOURCE_REVISION", "")
    evidence_bundle_id = os.getenv("RELEASE_EVIDENCE_BUNDLE_ID", "")
    evidence_bundle_sha256 = os.getenv("RELEASE_EVIDENCE_BUNDLE_SHA256", "")
    evidence_status = os.getenv("RELEASE_EVIDENCE_STATUS", "UNSET")

    return success_response(
        {
            "mode": context.state_machine.state.value,
            "kill_switch_active": context.state_machine.kill_switch_active,
            "market_data": "CONNECTED" if context.market_connected else "DISCONNECTED",
            "orchestrator": "RUNNING" if context.state_machine.can_operate() else "IDLE",
            "risk": "ACTIVE",
            "source_revision": context.settings.app_source_revision or None,
            "release_evidence": {
                "status": evidence_status,
                "source_revision": evidence_source_revision or None,
                "bundle_id": evidence_bundle_id or None,
                "bundle_sha256": evidence_bundle_sha256 or None,
                "matches_runtime_revision": bool(
                    context.settings.app_source_revision
                    and evidence_source_revision
                    and context.settings.app_source_revision == evidence_source_revision
                ),
            },
            "oms": {
                "environment": context.oms_service.target_environment.value,
                "exchange": context.oms_service.target_exchange.value,
                "live_execution_available": False,
            },
            "database": "CONNECTED" if context.repository is not None else "IN_MEMORY",
            "runtime_capacity": {
                "default_timeframe": context.settings.default_timeframe,
                "symbol_count": len(context.settings.allowed_symbols_list),
                "database_pool_size": context.settings.database_pool_size,
                "database_max_overflow": context.settings.database_max_overflow,
                "database_connection_ceiling": (
                    context.settings.database_pool_size
                    + context.settings.database_max_overflow
                ),
                "agent_max_concurrency": context.settings.agent_max_concurrency,
                "agent_worker_max_concurrency": (
                    context.settings.agent_worker_max_concurrency
                ),
                "agent_worker_batch_size": context.settings.agent_worker_batch_size,
                "agent_lease_seconds": context.settings.agent_lease_seconds,
            },
        }
    )

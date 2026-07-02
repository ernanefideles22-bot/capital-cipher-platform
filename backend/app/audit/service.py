"""Audit service (docs/02 Audit Layer, docs/12 audit_logs).

Critical rule: if an audit write fails on a critical event, the operation must
be blocked and the system should move to a safe state (docs/10, docs/31).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.errors import AuditError
from app.core.logging import ServiceLogger

logger = ServiceLogger("audit")


class AuditService:
    """Append-only audit trail. In-memory buffer + optional DB persistence."""

    def __init__(self, repository=None, max_buffer: int = 5000) -> None:
        self._repository = repository
        self._buffer: list[dict[str, Any]] = []
        self._max_buffer = max_buffer
        self.fail_mode: bool = False  # test hook: simulate audit failure

    async def record(
        self,
        *,
        correlation_id: str,
        audit_type: str,
        entity_type: str,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.fail_mode:
            raise AuditError(
                "Audit storage unavailable", correlation_id=correlation_id
            )
        record = {
            "audit_id": str(uuid4()),
            "correlation_id": correlation_id,
            "audit_type": audit_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._buffer.append(record)
        if len(self._buffer) > self._max_buffer:
            self._buffer.pop(0)
        if self._repository is not None:
            try:
                await self._repository.save_audit_log(record)
            except Exception as exc:
                logger.critical(
                    "Audit persistence failed",
                    event_type="AUDIT_LOG_FAILED",
                    correlation_id=correlation_id,
                    metadata={"error": str(exc)},
                )
                raise AuditError(
                    f"Audit persistence failed: {exc}", correlation_id=correlation_id
                ) from exc
        logger.info(
            f"Audit recorded: {audit_type}",
            event_type="AUDIT_LOG_CREATED",
            correlation_id=correlation_id,
            metadata={"entity_type": entity_type, "entity_id": entity_id},
        )
        return record

    def query(
        self,
        *,
        correlation_id: str | None = None,
        audit_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        results = self._buffer
        if correlation_id:
            results = [r for r in results if r["correlation_id"] == correlation_id]
        if audit_type:
            results = [r for r in results if r["audit_type"] == audit_type]
        return results[-limit:]

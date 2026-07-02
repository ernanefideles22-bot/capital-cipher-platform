"""Structured JSON logging (docs/20-observability.md, docs/09-coding-standards.md).

Every log line carries: timestamp, level, service, event_type, correlation_id,
message and metadata. Secrets must never be logged (docs/16-security-rules.md).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

SENSITIVE_KEYS = {"api_key", "apikey", "secret", "token", "password", "authorization"}


def _sanitize(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove obviously sensitive keys from log metadata."""
    return {k: ("***" if k.lower() in SENSITIVE_KEYS else v) for k, v in metadata.items()}


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", record.name),
            "event_type": getattr(record, "event_type", "LOG"),
            "correlation_id": getattr(record, "correlation_id", None),
            "message": record.getMessage(),
            "metadata": _sanitize(getattr(record, "metadata", {}) or {}),
        }
        if record.exc_info:
            payload["metadata"]["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


class ServiceLogger:
    """Logger wrapper enforcing the structured fields contract."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._logger = logging.getLogger(service)

    def log(
        self,
        level: int,
        message: str,
        *,
        event_type: str = "LOG",
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> None:
        self._logger.log(
            level,
            message,
            extra={
                "service": self._service,
                "event_type": event_type,
                "correlation_id": correlation_id,
                "metadata": metadata or {},
            },
            exc_info=exc_info,
        )

    def debug(self, message: str, **kw: Any) -> None:
        self.log(logging.DEBUG, message, **kw)

    def info(self, message: str, **kw: Any) -> None:
        self.log(logging.INFO, message, **kw)

    def warning(self, message: str, **kw: Any) -> None:
        self.log(logging.WARNING, message, **kw)

    def error(self, message: str, **kw: Any) -> None:
        self.log(logging.ERROR, message, **kw)

    def critical(self, message: str, **kw: Any) -> None:
        self.log(logging.CRITICAL, message, **kw)

"""Error taxonomy (docs/31-error-handling.md).

The system must fail safe: when in doubt, block the operation.
"""

from __future__ import annotations

from typing import Any


class CapitalCipherError(Exception):
    """Base class for all domain errors."""

    error_code: str = "INTERNAL_ERROR"
    severity: str = "ERROR"
    recoverable: bool = False

    def __init__(
        self,
        message: str,
        *,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.correlation_id = correlation_id
        self.metadata = metadata or {}

    def to_payload(self) -> dict[str, Any]:
        """Standard error format from docs/31-error-handling.md."""
        return {
            "error_code": self.error_code,
            "message": self.message,
            "severity": self.severity,
            "correlation_id": self.correlation_id,
            "recoverable": self.recoverable,
            "metadata": self.metadata,
        }


class ValidationError(CapitalCipherError):
    error_code = "VALIDATION_ERROR"
    recoverable = True


class DataQualityError(CapitalCipherError):
    error_code = "DATA_QUALITY_ERROR"


class MarketDataError(CapitalCipherError):
    error_code = "MARKET_DATA_UNAVAILABLE"
    recoverable = True


class AgentError(CapitalCipherError):
    error_code = "AGENT_FAILED"
    recoverable = True


class AgentTimeoutError(CapitalCipherError):
    error_code = "AGENT_TIMEOUT"
    recoverable = True


class RiskError(CapitalCipherError):
    error_code = "RISK_BLOCKED"
    severity = "CRITICAL"


class AuditError(CapitalCipherError):
    error_code = "AUDIT_FAILED"
    severity = "CRITICAL"


class DatabaseError(CapitalCipherError):
    error_code = "DATABASE_ERROR"
    severity = "CRITICAL"


class ConfigurationError(CapitalCipherError):
    error_code = "CONFIGURATION_ERROR"
    severity = "CRITICAL"


class SecurityError(CapitalCipherError):
    error_code = "SECURITY_ERROR"
    severity = "CRITICAL"


class SystemStateError(CapitalCipherError):
    error_code = "SYSTEM_NOT_READY"


class ExternalServiceError(CapitalCipherError):
    error_code = "EXTERNAL_SERVICE_ERROR"
    recoverable = True


class KillSwitchActiveError(CapitalCipherError):
    error_code = "KILL_SWITCH_ACTIVE"
    severity = "CRITICAL"

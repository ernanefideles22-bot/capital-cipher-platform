"""Fail-closed staging PAPER preflight and watchdog invariants."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ as process_environment
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from app.core.config import Settings

FORBIDDEN_TESTNET_CREDENTIALS = (
    "CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID",
    "CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET",
    "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID",
    "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET",
)
STAGING_TARGETS = {"LOCAL_COMPOSE", "HOSTED"}
SECURE_SSL_MODES = {"require", "verify-ca", "verify-full"}
FORBIDDEN_HOSTED_DATABASE_USERS = {
    "postgres",
    "supabase_admin",
    "supabase_auth_admin",
    "supabase_storage_admin",
}


@dataclass(frozen=True)
class StagingPreflightReport:
    environment: str
    deployment_target: str
    execution_environment: str
    market_data_enabled: bool
    event_broker_required: bool
    operations_monitor_enabled: bool
    database_tls_required: bool
    broker_tls_required: bool
    testnet_credentials_present: bool
    live_execution_available: bool = False

    def model_dump(self) -> dict:
        return asdict(self)


def load_staging_settings() -> Settings:
    """Load process settings without echoing secret-bearing validation input."""

    try:
        return Settings(_env_file=None)
    except Exception:
        raise RuntimeError("STAGING_SETTINGS_INVALID") from None


def _is_weak_secret(value: str | None) -> bool:
    if not value or len(value) < 32:
        return True
    lowered = value.lower()
    if any(token in lowered for token in ("change", "replace", "example", "placeholder")):
        return True
    return len(set(value)) < 8


def _database_tls_enabled(database_url: str) -> bool:
    query = parse_qs(urlsplit(database_url).query)
    configured = (query.get("sslmode") or query.get("ssl") or [""])[0].lower()
    return configured in SECURE_SSL_MODES


def validate_staging_environment(
    settings: Settings,
    environment: Mapping[str, str] | None = None,
) -> StagingPreflightReport:
    """Validate deployment-only invariants without exposing secret values."""

    values = environment if environment is not None else process_environment
    target = values.get("STAGING_DEPLOYMENT_TARGET", "").strip().upper()
    violations: list[str] = []
    if settings.app_env != "staging":
        violations.append("APP_ENV_NOT_STAGING")
    if target not in STAGING_TARGETS:
        violations.append("INVALID_DEPLOYMENT_TARGET")

    credentials_present = any(
        bool(values.get(name, "").strip())
        for name in FORBIDDEN_TESTNET_CREDENTIALS
    )
    if credentials_present:
        violations.append("TESTNET_CREDENTIAL_PRESENT")

    if _is_weak_secret(settings.admin_api_key):
        violations.append("WEAK_ADMIN_API_KEY")
    if _is_weak_secret(values.get("STAGING_POSTGRES_PASSWORD")):
        violations.append("WEAK_POSTGRES_PASSWORD")
    if _is_weak_secret(values.get("STAGING_REDIS_PASSWORD")):
        violations.append("WEAK_REDIS_PASSWORD")

    database = urlsplit(settings.database_url)
    broker = urlsplit(settings.redis_url or "")
    database_tls = _database_tls_enabled(settings.database_url)
    broker_tls = broker.scheme == "rediss"
    if target == "LOCAL_COMPOSE":
        if database.hostname != "db":
            violations.append("LOCAL_DATABASE_HOST_MUST_BE_DB")
        if broker.hostname != "redis":
            violations.append("LOCAL_REDIS_HOST_MUST_BE_REDIS")
    elif target == "HOSTED":
        if not database_tls:
            violations.append("HOSTED_DATABASE_REQUIRES_TLS")
        if not broker_tls:
            violations.append("HOSTED_REDIS_REQUIRES_TLS")
        if (database.username or "").lower() in FORBIDDEN_HOSTED_DATABASE_USERS:
            violations.append("HOSTED_DATABASE_PRIVILEGED_USER_FORBIDDEN")

    data_lake_value = values.get("DATA_LAKE_ROOT", "")
    data_lake_is_absolute = any(
        candidate.is_absolute()
        for candidate in (
            Path(data_lake_value),
            PurePosixPath(data_lake_value),
            PureWindowsPath(data_lake_value),
        )
    )
    if not data_lake_is_absolute:
        violations.append("DATA_LAKE_ROOT_MUST_BE_ABSOLUTE")

    if violations:
        raise RuntimeError(
            "Staging PAPER preflight failed: " + ",".join(sorted(violations))
        )
    return StagingPreflightReport(
        environment=settings.app_env,
        deployment_target=target,
        execution_environment=settings.oms_execution_environment,
        market_data_enabled=settings.enable_market_data,
        event_broker_required=settings.event_broker_required,
        operations_monitor_enabled=settings.operations_monitor_enabled,
        database_tls_required=target == "HOSTED",
        broker_tls_required=target == "HOSTED",
        testnet_credentials_present=credentials_present,
    )


def evaluate_watchdog_snapshot(
    readiness: Mapping,
    status_payload: Mapping,
    operations_payload: Mapping,
) -> list[str]:
    """Return stable violation codes for a running staging deployment."""

    violations: list[str] = []
    status_data = status_payload.get("data") or {}
    operations_data = operations_payload.get("data") or {}
    oms = status_data.get("oms") or {}
    recovery = operations_data.get("recovery") or {}
    if readiness.get("status") != "ready":
        violations.append("READINESS_FAILED")
    if readiness.get("live_execution_available") is not False:
        violations.append("READINESS_LIVE_BOUNDARY_INVALID")
    if status_data.get("mode") != "PAPER":
        violations.append("SYSTEM_NOT_PAPER")
    if oms.get("environment") != "PAPER":
        violations.append("OMS_NOT_PAPER")
    if oms.get("live_execution_available") is not False:
        violations.append("LIVE_EXECUTION_EXPOSED")
    if status_data.get("database") != "CONNECTED":
        violations.append("DATABASE_NOT_CONNECTED")
    if status_data.get("market_data") != "CONNECTED":
        violations.append("MARKET_DATA_NOT_CONNECTED")
    if status_data.get("kill_switch_active"):
        violations.append("KILL_SWITCH_ACTIVE")
    if operations_data.get("registered_agents") != 300:
        violations.append("AGENT_COHORT_INVALID")
    if not operations_data.get("decisions_allowed"):
        violations.append("DECISIONS_SUSPENDED")
    if not operations_data.get("shadow_admission_allowed"):
        violations.append("SHADOW_ADMISSION_SUSPENDED")
    if recovery.get("mode") != "HEALTHY":
        violations.append("RECOVERY_NOT_HEALTHY")
    return sorted(set(violations))

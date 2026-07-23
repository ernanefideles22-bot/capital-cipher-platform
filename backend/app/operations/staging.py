"""Fail-closed staging PAPER preflight and watchdog invariants."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ as process_environment
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlsplit

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
HOSTED_STAGING_SUPABASE_PROJECT_REF = "phkligpkcitbbefrrotk"
HOSTED_DATABASE_PORT = 5432
HOSTED_DATABASE_SSL_MODE = "verify-full"
HOSTED_DATABASE_CA_PATH = "/run/secrets/supabase-ca.crt"


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
    hosted_database_project_pinned: bool
    hosted_broker_host_pinned: bool
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


def _database_ca_is_pinned(database_url: str) -> bool:
    query = parse_qs(urlsplit(database_url).query)
    ssl_mode = (query.get("sslmode") or query.get("ssl") or [""])[0].lower()
    root_certificate = (query.get("sslrootcert") or [""])[0]
    return (
        ssl_mode == HOSTED_DATABASE_SSL_MODE
        and root_certificate == HOSTED_DATABASE_CA_PATH
    )


def _database_role(database_url: str) -> str:
    username = unquote(urlsplit(database_url).username or "").lower()
    project_suffix = f".{HOSTED_STAGING_SUPABASE_PROJECT_REF}"
    if username.endswith(project_suffix):
        return username[: -len(project_suffix)]
    return username


def _database_targets_hosted_staging(database_url: str) -> bool:
    database = urlsplit(database_url)
    hostname = (database.hostname or "").lower()
    username = unquote(database.username or "").lower()
    direct_hostname = (
        f"db.{HOSTED_STAGING_SUPABASE_PROJECT_REF}.supabase.co"
    )
    pooler_suffix = f".{HOSTED_STAGING_SUPABASE_PROJECT_REF}"
    return hostname == direct_hostname or (
        hostname.endswith(".pooler.supabase.com")
        and username.endswith(pooler_suffix)
    )


def _url_port(value: str, default: int) -> int | None:
    try:
        return urlsplit(value).port or default
    except ValueError:
        return None


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
    database = urlsplit(settings.database_url)
    broker = urlsplit(settings.redis_url or "")
    database_tls = _database_tls_enabled(settings.database_url)
    broker_tls = broker.scheme == "rediss"
    if target == "LOCAL_COMPOSE":
        if _is_weak_secret(values.get("STAGING_POSTGRES_PASSWORD")):
            violations.append("WEAK_POSTGRES_PASSWORD")
        if _is_weak_secret(values.get("STAGING_REDIS_PASSWORD")):
            violations.append("WEAK_REDIS_PASSWORD")
        if database.hostname != "db":
            violations.append("LOCAL_DATABASE_HOST_MUST_BE_DB")
        if broker.hostname != "redis":
            violations.append("LOCAL_REDIS_HOST_MUST_BE_REDIS")
    elif target == "HOSTED":
        if not database_tls:
            violations.append("HOSTED_DATABASE_REQUIRES_TLS")
        if not _database_ca_is_pinned(settings.database_url):
            violations.append("HOSTED_DATABASE_CA_REQUIRED")
        if not broker_tls:
            violations.append("HOSTED_REDIS_REQUIRES_TLS")
        if _database_role(settings.database_url) in FORBIDDEN_HOSTED_DATABASE_USERS:
            violations.append("HOSTED_DATABASE_PRIVILEGED_USER_FORBIDDEN")
        if _url_port(settings.database_url, HOSTED_DATABASE_PORT) != HOSTED_DATABASE_PORT:
            violations.append("HOSTED_DATABASE_SESSION_MODE_REQUIRED")
        if not _database_targets_hosted_staging(settings.database_url):
            violations.append("HOSTED_DATABASE_PROJECT_MISMATCH")
        if _is_weak_secret(unquote(database.password or "")):
            violations.append("HOSTED_DATABASE_WEAK_CREDENTIAL")
        if _is_weak_secret(unquote(broker.password or "")):
            violations.append("HOSTED_REDIS_WEAK_CREDENTIAL")
        expected_broker_host = values.get(
            "STAGING_EXPECTED_REDIS_HOST",
            "",
        ).strip().lower()
        if not expected_broker_host:
            violations.append("HOSTED_REDIS_HOST_NOT_PINNED")
        elif (broker.hostname or "").lower() != expected_broker_host:
            violations.append("HOSTED_REDIS_HOST_MISMATCH")

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
        hosted_database_project_pinned=(
            target == "HOSTED"
            and _database_targets_hosted_staging(settings.database_url)
        ),
        hosted_broker_host_pinned=(
            target == "HOSTED"
            and bool(values.get("STAGING_EXPECTED_REDIS_HOST", "").strip())
            and (broker.hostname or "").lower()
            == values.get("STAGING_EXPECTED_REDIS_HOST", "").strip().lower()
        ),
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

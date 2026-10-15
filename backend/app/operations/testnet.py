"""Fail-closed preflight for the isolated Bybit V5 TESTNET runtime.

The PAPER deployment deliberately cannot be switched to TESTNET by changing a
single environment variable.  TESTNET has its own resource namespace, runtime
entrypoint and explicit credential boundary.  This module validates those
invariants without ever returning credential material or connection strings.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ as process_environment
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from app.core.config import Settings

TESTNET_RESOURCE_NAMESPACE = "capital-cipher-bybit-testnet"
TESTNET_TARGETS = {"LOCAL_COMPOSE", "HOSTED"}
SECURE_SSL_MODES = {"require", "verify-ca", "verify-full"}
TESTNET_DATABASE_CA_PATH = "/run/secrets/supabase-testnet-ca.crt"

BYBIT_KEY_ID = "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID"
BYBIT_SIGNING_SECRET = "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET"
BINANCE_TESTNET_VARIABLES = (
    "CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID",
    "CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET",
)


@dataclass(frozen=True)
class TestnetPreflightReport:
    """Non-sensitive summary emitted by the TESTNET preflight."""

    environment: str
    deployment_target: str
    execution_environment: str
    exchange: str
    system_mode: str
    resource_namespace: str
    database_isolated: bool
    broker_isolated: bool
    database_tls_required: bool
    broker_tls_required: bool
    credentials_present: bool
    workers_enabled: bool
    reconciliation_enabled: bool
    live_execution_available: bool = False

    def model_dump(self) -> dict[str, object]:
        return asdict(self)


def load_testnet_settings() -> Settings:
    """Load only process environment; never read a developer ``.env`` file."""

    try:
        return Settings(_env_file=None)
    except Exception:
        # Pydantic errors may contain values copied from a URL.  Keep the
        # deployment log stable and free of credential-bearing diagnostics.
        raise RuntimeError("TESTNET_SETTINGS_INVALID") from None


def _strong_secret(value: str | None) -> bool:
    if not value or len(value) < 32:
        return False
    lowered = value.lower()
    if any(token in lowered for token in ("change", "replace", "example", "placeholder")):
        return False
    return len(set(value)) >= 8


def _credential_material(value: str | None, minimum_length: int) -> bool:
    """Validate exchange material without applying password-length rules to API IDs."""

    if not value or len(value.strip()) < minimum_length:
        return False
    lowered = value.lower()
    if any(token in lowered for token in ("change", "replace", "example", "placeholder")):
        return False
    return len(set(value.strip())) >= 6


def _tls_mode(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    return (query.get("sslmode") or query.get("ssl") or [""])[0].lower()


def _absolute_path(value: str) -> bool:
    return any(
        candidate.is_absolute()
        for candidate in (
            Path(value),
            PurePosixPath(value),
            PureWindowsPath(value),
        )
    )


def _resource_isolated(
    values: Mapping[str, str],
    testnet_name: str,
    staging_name: str,
) -> bool:
    testnet_resource = values.get(testnet_name, "").strip()
    staging_resource = values.get(staging_name, "").strip()
    if not testnet_resource:
        return False
    return not staging_resource or testnet_resource != staging_resource


def validate_testnet_environment(
    settings: Settings,
    environment: Mapping[str, str] | None = None,
) -> TestnetPreflightReport:
    """Validate TESTNET-only invariants without network access or secret output."""

    values = environment if environment is not None else process_environment
    violations: list[str] = []
    target = values.get("TESTNET_DEPLOYMENT_TARGET", "").strip().upper()
    namespace = values.get("TESTNET_RESOURCE_NAMESPACE", "").strip()

    if target not in TESTNET_TARGETS:
        violations.append("INVALID_DEPLOYMENT_TARGET")
    if namespace != TESTNET_RESOURCE_NAMESPACE:
        violations.append("INVALID_RESOURCE_NAMESPACE")
    if values.get("STAGING_DEPLOYMENT_TARGET", "").strip():
        violations.append("STAGING_DEPLOYMENT_TARGET_FORBIDDEN")

    if settings.app_env not in {"test", "dev"}:
        violations.append("APP_ENV_MUST_BE_TEST_OR_DEV")
    if settings.system_mode != "PAPER":
        violations.append("SYSTEM_MODE_MUST_BE_PAPER")
    if settings.oms_execution_environment != "TESTNET":
        violations.append("OMS_EXECUTION_ENVIRONMENT_MUST_BE_TESTNET")
    if not settings.oms_testnet_enabled:
        violations.append("OMS_TESTNET_ENABLED_REQUIRED")
    if settings.oms_testnet_acknowledgement != "TESTNET_ONLY_NO_REAL_FUNDS":
        violations.append("TESTNET_ACKNOWLEDGEMENT_INVALID")
    if settings.oms_testnet_exchange != "BYBIT":
        violations.append("OMS_TESTNET_EXCHANGE_MUST_BE_BYBIT")
    if settings.bybit_testnet_rest_url != "https://api-testnet.bybit.com":
        violations.append("BYBIT_TESTNET_URL_INVALID")
    if settings.bybit_testnet_category != "linear":
        violations.append("BYBIT_TESTNET_CATEGORY_INVALID")
    if settings.default_exchange != "BYBIT":
        violations.append("DEFAULT_EXCHANGE_MUST_BE_BYBIT")

    database = urlsplit(settings.database_url)
    broker = urlsplit(settings.redis_url or "")
    if database.scheme != "postgresql+asyncpg" or not database.hostname:
        violations.append("DATABASE_MUST_BE_POSTGRESQL")
    if broker.scheme not in {"redis", "rediss"} or not broker.hostname:
        violations.append("REDIS_URL_INVALID")
    if target == "LOCAL_COMPOSE":
        if database.hostname != "db":
            violations.append("LOCAL_DATABASE_HOST_MUST_BE_DB")
        if broker.hostname != "redis":
            violations.append("LOCAL_REDIS_HOST_MUST_BE_REDIS")
    elif target == "HOSTED":
        if database.hostname in {"db", "localhost", "127.0.0.1"}:
            violations.append("HOSTED_DATABASE_HOST_INVALID")
        if broker.hostname in {"redis", "localhost", "127.0.0.1"}:
            violations.append("HOSTED_REDIS_HOST_INVALID")
        if _tls_mode(settings.database_url) not in SECURE_SSL_MODES:
            violations.append("HOSTED_DATABASE_TLS_REQUIRED")
        if broker.scheme != "rediss":
            violations.append("HOSTED_REDIS_TLS_REQUIRED")
        root_certificate = (
            parse_qs(database.query).get("sslrootcert") or [""]
        )[0]
        if root_certificate != TESTNET_DATABASE_CA_PATH:
            violations.append("HOSTED_DATABASE_CA_REQUIRED")
        expected_database_host = values.get(
            "TESTNET_DATABASE_EXPECTED_HOST", ""
        ).strip().lower()
        expected_redis_host = values.get(
            "TESTNET_REDIS_EXPECTED_HOST", ""
        ).strip().lower()
        if not expected_database_host:
            violations.append("HOSTED_DATABASE_HOST_NOT_PINNED")
        elif database.hostname.lower() != expected_database_host:
            violations.append("HOSTED_DATABASE_HOST_MISMATCH")
        if not expected_redis_host:
            violations.append("HOSTED_REDIS_HOST_NOT_PINNED")
        elif broker.hostname.lower() != expected_redis_host:
            violations.append("HOSTED_REDIS_HOST_MISMATCH")

    if not _resource_isolated(
        values,
        "TESTNET_DATABASE_RESOURCE_ID",
        "STAGING_DATABASE_RESOURCE_ID",
    ):
        violations.append("DATABASE_RESOURCE_NOT_ISOLATED")
    if not _resource_isolated(
        values,
        "TESTNET_REDIS_RESOURCE_ID",
        "STAGING_REDIS_RESOURCE_ID",
    ):
        violations.append("REDIS_RESOURCE_NOT_ISOLATED")

    if not _credential_material(values.get(BYBIT_KEY_ID), 8):
        violations.append("BYBIT_KEY_MISSING_OR_WEAK")
    if not _credential_material(values.get(BYBIT_SIGNING_SECRET), 16):
        violations.append("BYBIT_SECRET_MISSING_OR_WEAK")
    if any(values.get(name, "").strip() for name in BINANCE_TESTNET_VARIABLES):
        violations.append("UNEXPECTED_BINANCE_CREDENTIALS")
    if not _strong_secret(settings.admin_api_key):
        violations.append("WEAK_ADMIN_API_KEY")
    if not settings.event_broker_required:
        violations.append("EVENT_BROKER_REQUIRED")
    if not settings.enable_market_data:
        violations.append("ENABLE_MARKET_DATA_REQUIRED")
    if not settings.agent_worker_enabled:
        violations.append("AGENT_WORKER_REQUIRED")
    if not settings.backfill_worker_enabled:
        violations.append("BACKFILL_WORKER_REQUIRED")
    if not settings.operations_monitor_enabled:
        violations.append("OPERATIONS_MONITOR_REQUIRED")
    if not settings.oms_worker_enabled:
        violations.append("OMS_WORKER_REQUIRED")
    if not settings.oms_reconciliation_enabled:
        violations.append("RECONCILIATION_REQUIRED")
    if not settings.oms_halt_on_critical_drift:
        violations.append("CRITICAL_DRIFT_HALT_REQUIRED")
    if settings.default_leverage != 1 or settings.max_leverage_simulated != 1:
        violations.append("TESTNET_LEVERAGE_MUST_BE_1X")
    if not _absolute_path(settings.data_lake_root):
        violations.append("DATA_LAKE_ROOT_MUST_BE_ABSOLUTE")
    if not settings.cors_allowed_origins_list or any(
        origin == "*" for origin in settings.cors_allowed_origins_list
    ):
        violations.append("CORS_MUST_BE_EXPLICIT")

    if violations:
        raise RuntimeError(
            "Bybit TESTNET preflight failed: " + ",".join(sorted(set(violations)))
        )

    return TestnetPreflightReport(
        environment=settings.app_env,
        deployment_target=target,
        execution_environment=settings.oms_execution_environment,
        exchange=settings.oms_testnet_exchange,
        system_mode=settings.system_mode,
        resource_namespace=namespace,
        database_isolated=True,
        broker_isolated=True,
        database_tls_required=target == "HOSTED",
        broker_tls_required=target == "HOSTED",
        credentials_present=True,
        workers_enabled=(settings.oms_worker_enabled and settings.agent_worker_enabled),
        reconciliation_enabled=settings.oms_reconciliation_enabled,
    )

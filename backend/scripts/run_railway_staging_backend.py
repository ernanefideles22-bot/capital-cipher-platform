"""Fail-closed Railway PAPER staging preflight before starting the API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.config import Settings

FORBIDDEN_TESTNET_CREDENTIALS = (
    "CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID",
    "CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET",
    "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID",
    "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET",
)
PRIVATE_SUFFIX = ".railway.internal"


def _weak(value: str | None) -> bool:
    if not value or len(value) < 32:
        return True
    lowered = value.lower()
    if any(token in lowered for token in ("change", "replace", "example", "placeholder")):
        return True
    return len(set(value)) < 8


def _require_private_endpoint(url: str, expected_host: str, port: int, label: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    expected = expected_host.strip().lower()
    if not expected or host != expected or not host.endswith(PRIVATE_SUFFIX):
        raise RuntimeError(f"RAILWAY_{label}_PRIVATE_HOST_INVALID")
    if (parsed.port or port) != port:
        raise RuntimeError(f"RAILWAY_{label}_PORT_INVALID")
    if _weak(unquote(parsed.password or "")):
        raise RuntimeError(f"RAILWAY_{label}_WEAK_CREDENTIAL")


def validate() -> dict[str, object]:
    try:
        settings = Settings(_env_file=None)
    except Exception:
        raise RuntimeError("RAILWAY_STAGING_SETTINGS_INVALID") from None

    violations: list[str] = []
    if settings.app_env != "staging":
        violations.append("APP_ENV_NOT_STAGING")
    if settings.system_mode != "PAPER":
        violations.append("SYSTEM_NOT_PAPER")
    if settings.oms_execution_environment != "PAPER":
        violations.append("OMS_NOT_PAPER")
    if settings.oms_testnet_enabled:
        violations.append("TESTNET_ENABLED")
    if any(os.getenv(name, "").strip() for name in FORBIDDEN_TESTNET_CREDENTIALS):
        violations.append("TESTNET_CREDENTIAL_PRESENT")
    if _weak(settings.admin_api_key):
        violations.append("WEAK_ADMIN_API_KEY")
    if not settings.event_broker_required:
        violations.append("EVENT_BROKER_NOT_REQUIRED")
    if not settings.enable_market_data:
        violations.append("MARKET_DATA_DISABLED")
    if not settings.operations_monitor_enabled:
        violations.append("OPERATIONS_MONITOR_DISABLED")
    if not Path(os.getenv("DATA_LAKE_ROOT", "")).is_absolute():
        violations.append("DATA_LAKE_ROOT_MUST_BE_ABSOLUTE")

    if violations:
        raise RuntimeError("Railway PAPER preflight failed: " + ",".join(sorted(violations)))

    _require_private_endpoint(
        settings.database_url,
        os.getenv("STAGING_EXPECTED_DATABASE_HOST", ""),
        5432,
        "DATABASE",
    )
    _require_private_endpoint(
        settings.redis_url or "",
        os.getenv("STAGING_EXPECTED_REDIS_HOST", ""),
        6379,
        "REDIS",
    )

    return {
        "event": "RAILWAY_STAGING_PAPER_RUNTIME_AUTHORIZED",
        "environment": settings.app_env,
        "mode": settings.system_mode,
        "execution_environment": settings.oms_execution_environment,
        "market_data_enabled": settings.enable_market_data,
        "event_broker_required": settings.event_broker_required,
        "live_execution_available": False,
        "testnet_credentials_present": False,
    }


def main() -> None:
    print(json.dumps(validate(), sort_keys=True), flush=True)
    port = os.getenv("PORT", "8000")
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            port,
            "--workers",
            "1",
        ],
    )


if __name__ == "__main__":
    main()

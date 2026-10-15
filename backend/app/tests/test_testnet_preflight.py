"""Tests for the isolated Bybit TESTNET deployment boundary."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.operations.testnet import (
    TESTNET_RESOURCE_NAMESPACE,
    validate_testnet_environment,
)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "APP_ENV": "test",
        "SYSTEM_MODE": "PAPER",
        "TESTNET_DEPLOYMENT_TARGET": "LOCAL_COMPOSE",
        "TESTNET_RESOURCE_NAMESPACE": TESTNET_RESOURCE_NAMESPACE,
        "TESTNET_DATABASE_RESOURCE_ID": "local-testnet-postgres",
        "TESTNET_REDIS_RESOURCE_ID": "local-testnet-redis",
        "STAGING_DATABASE_RESOURCE_ID": "local-staging-postgres",
        "STAGING_REDIS_RESOURCE_ID": "local-staging-redis",
        "DATABASE_URL": "postgresql+asyncpg://testnet:password@db:5432/testnet",
        "REDIS_URL": "redis://:password@redis:6379/0",
        "EVENT_BROKER_REQUIRED": True,
        "ENABLE_MARKET_DATA": True,
        "DATA_LAKE_ROOT": "/var/lib/capital-cipher/data-lake",
        "ADMIN_API_KEY": "admin-key-that-is-long-enough-for-testnet-1234",
        "OMS_EXECUTION_ENVIRONMENT": "TESTNET",
        "OMS_TESTNET_ENABLED": True,
        "OMS_TESTNET_ACKNOWLEDGEMENT": "TESTNET_ONLY_NO_REAL_FUNDS",
        "OMS_TESTNET_EXCHANGE": "BYBIT",
        "BYBIT_TESTNET_REST_URL": "https://api-testnet.bybit.com",
        "BYBIT_TESTNET_CATEGORY": "linear",
        "DEFAULT_EXCHANGE": "BYBIT",
        "OMS_WORKER_ENABLED": True,
        "OMS_RECONCILIATION_ENABLED": True,
        "OMS_HALT_ON_CRITICAL_DRIFT": True,
        "OPERATIONS_MONITOR_ENABLED": True,
        "AGENT_WORKER_ENABLED": True,
        "BACKFILL_WORKER_ENABLED": True,
        "DEFAULT_LEVERAGE": 1,
        "MAX_LEVERAGE_SIMULATED": 1,
    }
    values.update(overrides)
    return Settings(**values)


def _environment(**overrides) -> dict[str, str]:
    values = {
        "TESTNET_DEPLOYMENT_TARGET": "LOCAL_COMPOSE",
        "TESTNET_RESOURCE_NAMESPACE": TESTNET_RESOURCE_NAMESPACE,
        "TESTNET_DATABASE_RESOURCE_ID": "local-testnet-postgres",
        "TESTNET_REDIS_RESOURCE_ID": "local-testnet-redis",
        "STAGING_DATABASE_RESOURCE_ID": "local-staging-postgres",
        "STAGING_REDIS_RESOURCE_ID": "local-staging-redis",
        "CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID": "bybit-testnet-key-id-1234567890-abcd",
        "CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET": "bybit-testnet-signing-secret-1234567890",
    }
    values.update(overrides)
    return values


def test_local_bybit_testnet_preflight_passes_without_exposing_secrets():
    report = validate_testnet_environment(_settings(), _environment())

    assert report.exchange == "BYBIT"
    assert report.resource_namespace == TESTNET_RESOURCE_NAMESPACE
    assert report.credentials_present is True
    assert report.live_execution_available is False
    assert "bybit-testnet-key-id" not in repr(report)


def test_testnet_preflight_rejects_shared_resource_identity():
    with pytest.raises(RuntimeError, match="DATABASE_RESOURCE_NOT_ISOLATED"):
        validate_testnet_environment(
            _settings(),
            _environment(TESTNET_DATABASE_RESOURCE_ID="local-staging-postgres"),
        )


def test_testnet_preflight_rejects_missing_bybit_credentials():
    with pytest.raises(RuntimeError, match="BYBIT_KEY_MISSING_OR_WEAK"):
        validate_testnet_environment(
            _settings(),
            _environment(CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID=""),
        )


def test_hosted_testnet_requires_tls_and_ca_pin():
    settings = _settings(
        TESTNET_DEPLOYMENT_TARGET="HOSTED",
        DATABASE_URL="postgresql+asyncpg://testnet:password@db.example:5432/testnet",
        REDIS_URL="redis://:password@redis.example:6379/0",
    )
    with pytest.raises(RuntimeError, match="HOSTED_DATABASE_TLS_REQUIRED"):
        validate_testnet_environment(
            settings,
            _environment(TESTNET_DEPLOYMENT_TARGET="HOSTED"),
        )


def test_hosted_testnet_requires_pinned_resource_hosts():
    settings = _settings(
        TESTNET_DEPLOYMENT_TARGET="HOSTED",
        DATABASE_URL=(
            "postgresql+asyncpg://testnet:password@db.example:5432/testnet"
            "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-testnet-ca.crt"
        ),
        REDIS_URL="rediss://:password@redis.example:6379/0",
    )
    with pytest.raises(RuntimeError, match="HOSTED_DATABASE_HOST_NOT_PINNED"):
        validate_testnet_environment(
            settings,
            _environment(TESTNET_DEPLOYMENT_TARGET="HOSTED"),
        )

"""Staging must remain a credential-free, observable PAPER environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.operations.staging import (
    evaluate_watchdog_snapshot,
    validate_staging_environment,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STAGING_PROJECT_REF = "phkligpkcitbbefrrotk"
HOSTED_REDIS_HOST = "redis-staging.example.invalid"


def staging_settings(**overrides) -> Settings:
    values = {
        "APP_ENV": "staging",
        "SYSTEM_MODE": "PAPER",
        "OMS_EXECUTION_ENVIRONMENT": "PAPER",
        "OMS_TESTNET_ENABLED": False,
        "OMS_TESTNET_ACKNOWLEDGEMENT": "",
        "OMS_WORKER_ENABLED": False,
        "OMS_RECONCILIATION_ENABLED": False,
        "DATABASE_URL": (
            "postgresql+asyncpg://capital_cipher:secret@db:5432/"
            "capital_cipher_staging"
        ),
        "REDIS_URL": "redis://:secret@redis:6379/0",
        "EVENT_BROKER_REQUIRED": True,
        "ENABLE_MARKET_DATA": True,
        "OPERATIONS_MONITOR_ENABLED": True,
        "AGENT_WORKER_ENABLED": True,
        "BACKFILL_WORKER_ENABLED": True,
        "ADMIN_API_KEY": "admin-key-abcdefghijklmnopqrstuvwxyz-0123456789",
        "DEFAULT_LEVERAGE": 1,
        "MAX_LEVERAGE_SIMULATED": 1,
        "CORS_ALLOWED_ORIGINS": "https://staging.example.invalid",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def local_environment(**overrides) -> dict[str, str]:
    values = {
        "STAGING_DEPLOYMENT_TARGET": "LOCAL_COMPOSE",
        "STAGING_POSTGRES_PASSWORD": "postgres-abcdefghijklmnopqrstuvwxyz-012345",
        "STAGING_REDIS_PASSWORD": "redis-abcdefghijklmnopqrstuvwxyz-0123456789",
        "DATA_LAKE_ROOT": "/var/lib/capital-cipher/data-lake",
    }
    values.update(overrides)
    return values


def hosted_environment(**overrides) -> dict[str, str]:
    values = {
        "STAGING_DEPLOYMENT_TARGET": "HOSTED",
        "STAGING_EXPECTED_REDIS_HOST": HOSTED_REDIS_HOST,
        "DATA_LAKE_ROOT": "/var/lib/capital-cipher/data-lake",
    }
    values.update(overrides)
    return values


def hosted_settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": (
            "postgresql+asyncpg://capital_cipher_staging."
            f"{STAGING_PROJECT_REF}:"
            "database-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
            "aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
            "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
        ),
        "REDIS_URL": (
            "rediss://default:"
            "redis-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
            f"{HOSTED_REDIS_HOST}:6379/0"
        ),
    }
    values.update(overrides)
    return staging_settings(**values)


def test_staging_preflight_accepts_only_complete_paper_boundary():
    report = validate_staging_environment(
        staging_settings(),
        local_environment(),
    )

    assert report.environment == "staging"
    assert report.deployment_target == "LOCAL_COMPOSE"
    assert report.execution_environment == "PAPER"
    assert report.market_data_enabled is True
    assert report.testnet_credentials_present is False
    assert report.live_execution_available is False


@pytest.mark.parametrize(
    "override",
    [
        {"OMS_EXECUTION_ENVIRONMENT": "TESTNET"},
        {"OMS_TESTNET_ENABLED": True},
        {"OMS_WORKER_ENABLED": True},
        {"OMS_RECONCILIATION_ENABLED": True},
        {"ENABLE_MARKET_DATA": False},
        {"EVENT_BROKER_REQUIRED": False},
        {"OPERATIONS_MONITOR_ENABLED": False},
        {"MAX_LEVERAGE_SIMULATED": 5},
        {"ADMIN_API_KEY": ""},
        {"CORS_ALLOWED_ORIGINS": "*"},
        {"DATABASE_POOL_SIZE": 10, "DATABASE_MAX_OVERFLOW": 1},
    ],
)
def test_staging_settings_fail_closed(override):
    with pytest.raises(ValueError):
        staging_settings(**override)


def test_staging_preflight_rejects_exchange_credentials_without_echoing_them():
    secret = "must-never-appear-in-error-output"
    environment = local_environment(
        CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET=secret,
    )

    with pytest.raises(RuntimeError) as raised:
        validate_staging_environment(staging_settings(), environment)

    assert "TESTNET_CREDENTIAL_PRESENT" in str(raised.value)
    assert secret not in str(raised.value)


def test_hosted_staging_requires_tls_for_postgres_and_redis():
    hosted = hosted_environment()
    with pytest.raises(RuntimeError) as raised:
        validate_staging_environment(staging_settings(), hosted)
    assert "HOSTED_DATABASE_REQUIRES_TLS" in str(raised.value)
    assert "HOSTED_REDIS_REQUIRES_TLS" in str(raised.value)

    report = validate_staging_environment(hosted_settings(), hosted)
    assert report.database_tls_required is True
    assert report.broker_tls_required is True
    assert report.hosted_database_project_pinned is True
    assert report.hosted_broker_host_pinned is True


def test_hosted_staging_rejects_privileged_database_users():
    settings = hosted_settings(
        DATABASE_URL=(
            f"postgresql+asyncpg://postgres.{STAGING_PROJECT_REF}:"
            "database-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
            "aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
            "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
        ),
    )

    with pytest.raises(RuntimeError) as raised:
        validate_staging_environment(settings, hosted_environment())

    assert "HOSTED_DATABASE_PRIVILEGED_USER_FORBIDDEN" in str(raised.value)


@pytest.mark.parametrize(
    ("settings_overrides", "environment_overrides", "violation"),
    [
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://capital_cipher_staging.wrongproject:"
                    "database-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
                    "aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
                    "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
                )
            },
            {},
            "HOSTED_DATABASE_PROJECT_MISMATCH",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://capital_cipher_staging."
                    f"{STAGING_PROJECT_REF}:"
                    "database-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
                    "aws-0-sa-east-1.pooler.supabase.com:6543/postgres"
                    "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
                )
            },
            {},
            "HOSTED_DATABASE_SESSION_MODE_REQUIRED",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://capital_cipher_staging."
                    f"{STAGING_PROJECT_REF}:"
                    "database-password-abcdefghijklmnopqrstuvwxyz-0123456789@"
                    "aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
                    "?sslmode=require"
                )
            },
            {},
            "HOSTED_DATABASE_CA_REQUIRED",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql+asyncpg://capital_cipher_staging."
                    f"{STAGING_PROJECT_REF}:weak@"
                    "aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
                    "?sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
                )
            },
            {},
            "HOSTED_DATABASE_WEAK_CREDENTIAL",
        ),
        (
            {"REDIS_URL": f"rediss://default:weak@{HOSTED_REDIS_HOST}:6379/0"},
            {},
            "HOSTED_REDIS_WEAK_CREDENTIAL",
        ),
        (
            {},
            {"STAGING_EXPECTED_REDIS_HOST": "different.example.invalid"},
            "HOSTED_REDIS_HOST_MISMATCH",
        ),
        (
            {},
            {"STAGING_EXPECTED_REDIS_HOST": ""},
            "HOSTED_REDIS_HOST_NOT_PINNED",
        ),
    ],
)
def test_hosted_staging_fails_closed_on_endpoint_or_credential_drift(
    settings_overrides,
    environment_overrides,
    violation,
):
    with pytest.raises(RuntimeError) as raised:
        validate_staging_environment(
            hosted_settings(**settings_overrides),
            hosted_environment(**environment_overrides),
        )

    assert violation in str(raised.value)
    assert "database-password-abcdefghijklmnopqrstuvwxyz-0123456789" not in str(
        raised.value
    )


def healthy_watchdog_payloads() -> tuple[dict, dict, dict]:
    readiness = {"status": "ready", "live_execution_available": False}
    status_payload = {
        "data": {
            "mode": "PAPER",
            "kill_switch_active": False,
            "market_data": "CONNECTED",
            "database": "CONNECTED",
            "oms": {
                "environment": "PAPER",
                "live_execution_available": False,
            },
        }
    }
    operations_payload = {
        "data": {
            "registered_agents": 300,
            "decisions_allowed": True,
            "shadow_admission_allowed": True,
            "recovery": {"mode": "HEALTHY"},
        }
    }
    return readiness, status_payload, operations_payload


def test_watchdog_accepts_only_healthy_300_agent_paper_snapshot():
    payloads = healthy_watchdog_payloads()
    assert evaluate_watchdog_snapshot(*payloads) == []

    readiness, status_payload, operations_payload = payloads
    status_payload["data"]["oms"]["environment"] = "TESTNET"
    status_payload["data"]["market_data"] = "DISCONNECTED"
    operations_payload["data"]["registered_agents"] = 299
    operations_payload["data"]["recovery"]["mode"] = "DEGRADED"

    assert evaluate_watchdog_snapshot(
        readiness,
        status_payload,
        operations_payload,
    ) == [
        "AGENT_COHORT_INVALID",
        "MARKET_DATA_NOT_CONNECTED",
        "OMS_NOT_PAPER",
        "RECOVERY_NOT_HEALTHY",
    ]


def test_staging_compose_is_loopback_only_and_initializes_supabase_migrations():
    compose = (REPOSITORY_ROOT / "deploy" / "staging" / "compose.yml").read_text()
    dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text()

    assert "postgres:17-alpine" in compose
    assert "../../supabase/migrations:/docker-entrypoint-initdb.d:ro" in compose
    assert '127.0.0.1:${STAGING_BACKEND_PORT:-8000}:8000' in compose
    assert "OMS_EXECUTION_ENVIRONMENT: PAPER" in compose
    assert 'OMS_TESTNET_ENABLED: "0"' in compose
    assert "capital-cipher/data-lake" in compose
    assert "USER 10001:10001" in dockerfile


def test_hosted_compose_uses_only_external_pinned_data_services():
    compose = (
        REPOSITORY_ROOT / "deploy" / "staging" / "compose.hosted.yml"
    ).read_text()

    assert "STAGING_DEPLOYMENT_TARGET: HOSTED" in compose
    assert "STAGING_EXPECTED_REDIS_HOST" in compose
    assert "DATABASE_POOL_SIZE" in compose
    assert "DATABASE_MAX_OVERFLOW" in compose
    assert "STAGING_POSTGRES_PASSWORD" not in compose
    assert "STAGING_REDIS_PASSWORD" not in compose
    assert "  db:" not in compose
    assert "  redis:" not in compose
    assert '127.0.0.1:${STAGING_BACKEND_PORT:-8000}:8000' in compose
    assert "hosted-data-lake:/var/lib/capital-cipher/data-lake" in compose
    assert "network_mode: none" in compose
    assert "sslrootcert=/run/secrets/supabase-ca.crt" in (
        REPOSITORY_ROOT / "deploy" / "staging" / ".env.hosted.example"
    ).read_text()
    assert "SUPABASE_CA_CERT_HOST_PATH" in compose
    assert compose.count("- supabase-ca") == 2
    assert compose.count("cap_drop: [ALL]") == 3

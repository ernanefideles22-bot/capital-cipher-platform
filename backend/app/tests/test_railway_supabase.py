"""Destination drift must fail before the existing PAPER service can connect."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import ssl

import pytest

from app.operations import railway_supabase as gate
from app.tests.test_staging_paper import staging_settings

HOST = f"db.{gate.PROJECT_REF}.supabase.co"
PASSWORD = "dB-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789"


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    ca = tmp_path / "ca.crt"
    ca.write_text(Path(ssl.get_default_verify_paths().cafile).read_text())
    monkeypatch.setattr(gate, "CA_PATH", ca)
    url = f"postgresql+asyncpg://{gate.DATABASE_ROLE}:{PASSWORD}@{HOST}:5432/postgres?sslmode=verify-full&sslrootcert={ca}"
    return url, {"STAGING_EXPECTED_DATABASE_HOST": HOST}, ca


def test_direct_connection_requires_verified_ca_and_least_privilege(boundary):
    url, env, _ = boundary
    gate.validate_supabase_database(staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=4), env)


def test_pinned_session_pooler_preserves_project_role_and_tls(boundary):
    url, env, _ = boundary
    pooler = "aws-99-sa-east-1.pooler.supabase.com"  # fixture only; not a discovered endpoint
    url = url.replace(gate.DATABASE_ROLE + ":", gate.DATABASE_ROLE + "." + gate.PROJECT_REF + ":").replace(HOST, pooler)
    gate.validate_supabase_database(staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=4), {**env, "STAGING_EXPECTED_DATABASE_HOST": pooler})


@pytest.mark.parametrize("transform", [
    lambda u: u.replace(HOST, "db.another-project.supabase.co"),
    lambda u: u.replace(":5432/", ":6543/"),
    lambda u: u.replace("/postgres?", "/another_database?"),
    lambda u: u.replace(gate.DATABASE_ROLE + ":", "postgres:"),
    lambda u: u.replace("verify-full", "require"),
    lambda u: u + "&ssl=disable",
    lambda u: u + "&sslmode=disable",
    lambda u: u + "&host=attacker.invalid",
    lambda u: u + "#fragment",
    lambda u: u.replace(PASSWORD, "weak"),
])
def test_destination_tls_or_role_drift_is_rejected_without_echoing_password(boundary, transform):
    url, env, _ = boundary
    with pytest.raises(RuntimeError) as error:
        gate.validate_supabase_database(staging_settings(DATABASE_URL=transform(url), DATABASE_POOL_SIZE=4), env)
    assert PASSWORD not in str(error.value)


@pytest.mark.parametrize("pool,overflow", [(5, 0), (4, 1), (8, 2)])
def test_connection_budget_leaves_headroom_for_validation(boundary, pool, overflow):
    url, env, _ = boundary
    with pytest.raises(RuntimeError):
        gate.validate_supabase_database(staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=pool, DATABASE_MAX_OVERFLOW=overflow), env)


def test_missing_or_symlink_ca_is_rejected(boundary):
    url, env, ca = boundary
    ca.unlink()
    with pytest.raises(RuntimeError):
        gate.validate_supabase_database(staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=4), env)
    ca.symlink_to("/etc/ssl/certs/ca-certificates.crt")
    with pytest.raises(RuntimeError):
        gate.validate_supabase_database(staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=4), env)


def test_ca_materialization_rejects_invalid_input_without_writing(boundary):
    _, _, ca = boundary
    previous = ca.read_bytes()
    with pytest.raises(RuntimeError, match="SUPABASE_CA_INVALID"):
        gate.prepare_ca({"SUPABASE_CA_CERT_B64": "invalid!"})
    assert ca.read_bytes() == previous


def test_ca_materialization_accepts_verified_public_certificate(boundary):
    _, _, ca = boundary
    certificate = ca.read_bytes()
    gate.prepare_ca({"SUPABASE_CA_CERT_B64": base64.b64encode(certificate).decode()})
    assert ca.read_bytes() == certificate
    assert ca.stat().st_mode & 0o777 == 0o600


def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/run_railway_staging_backend.py"
    spec = importlib.util.spec_from_file_location("railway_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_railway_default_is_retained(monkeypatch):
    module = launcher()
    settings = staging_settings(DATABASE_URL=f"postgresql+asyncpg://capital_cipher_app:{PASSWORD}@postgreseu2.railway.internal:5432/railway", REDIS_URL=f"redis://:{PASSWORD}@rediseu.railway.internal:6379/0")
    monkeypatch.setattr(module, "Settings", lambda **_: settings)
    monkeypatch.delenv("STAGING_DATABASE_PROVIDER", raising=False)
    monkeypatch.setenv("STAGING_EXPECTED_DATABASE_HOST", "postgreseu2.railway.internal")
    monkeypatch.setenv("STAGING_EXPECTED_REDIS_HOST", "rediseu.railway.internal")
    monkeypatch.setenv("DATA_LAKE_ROOT", "/var/lib/capital-cipher/data-lake")
    assert module.validate()["mode"] == "PAPER"


def test_explicit_supabase_option_keeps_redis_private_and_paper_only(boundary, monkeypatch):
    url, env, _ = boundary
    module = launcher()
    settings = staging_settings(DATABASE_URL=url, DATABASE_POOL_SIZE=4, REDIS_URL=f"redis://:{PASSWORD}@rediseu.railway.internal:6379/0")
    monkeypatch.setattr(module, "Settings", lambda **_: settings)
    for k,v in {**env,"STAGING_DATABASE_PROVIDER":"supabase","STAGING_EXPECTED_REDIS_HOST":"rediseu.railway.internal","DATA_LAKE_ROOT":"/var/lib/capital-cipher/data-lake"}.items():
        monkeypatch.setenv(k,v)
    monkeypatch.delenv("SUPABASE_CA_CERT_B64", raising=False)
    assert module.validate()["live_execution_available"] is False
    monkeypatch.setenv("CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID", "forbidden-fixture")
    with pytest.raises(RuntimeError, match="TESTNET_CREDENTIAL_PRESENT"):
        module.validate()

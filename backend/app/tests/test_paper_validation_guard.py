"""The native deployment gate must refuse original or arbitrary databases."""
from pathlib import Path
import importlib

import pytest


def gate_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("validate_railway_paper_flow")


@pytest.mark.parametrize("name", ["capital_cipher_staging", "postgres", "capital_cipher_infra_smoke_20261023"])
def test_gate_refuses_runtime_and_donor_as_write_targets(monkeypatch, name):
    gate = gate_module(monkeypatch)
    with pytest.raises(RuntimeError, match="DATABASE_NAME_REFUSED"):
        gate.database_url("postgresql://localhost/postgres", name)


async def test_gate_requires_explicit_opt_in_before_any_connection(monkeypatch):
    gate = gate_module(monkeypatch)
    monkeypatch.delenv("PAPER_FULL_FLOW_VALIDATION_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="OPT_IN_REQUIRED"):
        await gate.main()


@pytest.mark.parametrize("office,pool,batch", [("false", 2, 32), ("true", 4, 1)])
def test_validation_pool_supports_office_without_changing_live_connection(monkeypatch, office, pool, batch):
    gate = gate_module(monkeypatch)
    original = "postgresql+asyncpg://capital_cipher_app:fixture@capital-cipher-recovered.railway.internal:5432/capital_cipher_staging"
    monkeypatch.setenv("DATABASE_URL", original)
    monkeypatch.setenv("REDIS_URL", "redis://fixture.invalid/0")
    monkeypatch.setenv("OFFICE_ASYNC_ENABLED", office)
    monkeypatch.setenv("DATABASE_POOL_SIZE", "4")
    name = gate.PREFIX + "a" * 12 + "_flow"
    settings = gate.validation_settings(name, "a" * 40)
    assert settings.database_pool_size == pool
    assert settings.database_max_overflow == 0
    assert settings.agent_worker_batch_size == batch
    assert settings.oms_execution_environment == "PAPER"
    assert settings.database_url.endswith("/" + name)
    assert gate.os.environ["DATABASE_URL"] == original


async def test_invalid_validation_configuration_stops_before_database_connection(monkeypatch):
    gate = gate_module(monkeypatch)
    monkeypatch.setenv("PAPER_FULL_FLOW_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgresql://fixture@capital-cipher-recovered.railway.internal/capital_cipher_staging")
    monkeypatch.setenv("STAGING_EXPECTED_DATABASE_HOST", gate.HOST)
    monkeypatch.setenv("APP_SOURCE_REVISION", "a" * 40)
    monkeypatch.setattr(gate, "validate_staging", lambda: None)
    real_path = gate.Path
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(gate, "Path", lambda path: root / "supabase/migrations" if path == "/app/migrations" else real_path(path))
    monkeypatch.setattr(gate, "validation_settings", lambda *args: (_ for _ in ()).throw(ValueError("invalid isolated pool")))
    attempted = []
    async def connect(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("No network allowed before settings validation")
    monkeypatch.setattr(gate.asyncpg, "connect", connect)
    with pytest.raises(ValueError, match="invalid isolated pool"):
        await gate.main()
    assert attempted == []


@pytest.mark.parametrize("limit,superuser", [(7, False), (8, True), (8, False)])
async def test_live_readonly_guard_rejects_connection_or_privilege_drift(monkeypatch, limit, superuser):
    gate = gate_module(monkeypatch)
    from types import SimpleNamespace
    async def invariants(connection): return {"tables": 55}
    monkeypatch.setattr(gate, "invariants", invariants)
    monkeypatch.setattr(gate, "Settings", lambda **kwargs: SimpleNamespace(database_pool_size=4, database_max_overflow=0))
    class ReadOnlyTransaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"readonly": True}
            return ReadOnlyTransaction()
        async def fetch(self, sql): return []
        async def fetchrow(self, sql):
            return dict(rolconnlimit=limit, rolsuper=superuser, rolcreatedb=False,
                rolcreaterole=False, rolreplication=False, rolbypassrls=False)
    if limit == 8 and not superuser:
        result = await gate.verify_live_compatibility(Connection(), [], SimpleNamespace(database_pool_size=4))
        assert result["backend_connection_ceiling"] + result["isolated_connection_ceiling"] == 8
        assert result["main_database_writes"] is False
    else:
        with pytest.raises(RuntimeError, match="BUDGET_EXCEEDED|PRIVILEGE_DRIFT"):
            await gate.verify_live_compatibility(Connection(), [], SimpleNamespace(database_pool_size=4))


async def test_live_migration_hash_drift_stops_readonly_verification(monkeypatch, tmp_path):
    gate = gate_module(monkeypatch)
    migration = tmp_path / "fixture.sql"
    migration.write_text("select 1;")
    async def invariants(connection): return {"tables": 55}
    monkeypatch.setattr(gate, "invariants", invariants)
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    class Connection:
        def transaction(self, **kwargs):
            assert kwargs == {"readonly": True}
            return Transaction()
        async def fetch(self, sql):
            assert sql.lstrip().startswith("select")
            return [{"filename": "fixture.sql", "sha256": "wrong"}]
    with pytest.raises(RuntimeError, match="LIVE_MIGRATION_HASH_MISMATCH"):
        await gate.verify_live_compatibility(Connection(), [migration], None)

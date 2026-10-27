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


def test_native_failure_diagnostics_identify_checkpoint_without_secret_values(monkeypatch):
    import json
    from app.validation.paper_flow import PaperFlowValidationError
    gate = gate_module(monkeypatch)
    exc = PaperFlowValidationError([{"error": "PRIMARY_DEADLINE_EXCEEDED"},
                                   {"error": "postgresql://user:secret@private/db"}], 3010.5)
    result = gate.failure_report(exc)
    assert result["stage"] == "decision_cycle"
    assert result["error_codes"] == ["OTHER", "PRIMARY_DEADLINE_EXCEEDED"]
    assert result["elapsed_ms"] == 3010.5
    assert "secret" not in json.dumps(result) and "postgresql" not in json.dumps(result)


def test_generic_failure_exposes_source_coordinate_not_exception_or_sql(monkeypatch):
    import json
    gate = gate_module(monkeypatch)
    try:
        raise AssertionError("postgresql://user:secret@private/db")
    except AssertionError as exc:
        result = gate.failure_report(exc)
    assert result["source_locations"][-1]["function"] == "test_generic_failure_exposes_source_coordinate_not_exception_or_sql"
    assert result["source_locations"][-1]["line"] > 0
    assert "secret" not in json.dumps(result) and "postgresql" not in json.dumps(result)


@pytest.mark.parametrize("actual,expected", [("sfo", "sfo"), ("europe-west4-drams3a", "europe-west4-drams3a")])
def test_regional_guard_accepts_exact_approved_location(monkeypatch, actual, expected):
    gate = gate_module(monkeypatch)
    monkeypatch.setenv("APP_SOURCE_REVISION", "c" * 40)
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", actual)
    monkeypatch.setenv("PAPER_VALIDATION_EXPECTED_REGION", expected)
    identity = gate.runtime_identity()
    gate.require_expected_region(identity)
    assert identity == {"source_revision": "c" * 40, "runtime_region": actual, "expected_region": expected}


@pytest.mark.parametrize("actual,expected", [("europe-west4-drams3a", "sfo"), ("", "sfo"), ("sfo", ""),
    ("sfo", "postgresql://user:secret@db"), ("secret/region", "sfo")])
async def test_wrong_or_unverified_region_stops_before_connections(monkeypatch, actual, expected):
    gate = gate_module(monkeypatch)
    monkeypatch.setenv("PAPER_FULL_FLOW_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", actual)
    monkeypatch.setenv("PAPER_VALIDATION_EXPECTED_REGION", expected)
    async def forbidden(*args, **kwargs):
        pytest.fail("A mismatched regional experiment must not reach PostgreSQL")
    monkeypatch.setattr(gate.asyncpg, "connect", forbidden)
    monkeypatch.setattr(gate, "validate_staging", lambda: pytest.fail("Region must be checked before staging validation"))
    with pytest.raises(gate.ValidationRegionError) as failure:
        await gate.main()
    import json
    report = gate.failure_report(failure.value)
    assert report["error_codes"] == ["PAPER_VALIDATION_REGION_MISMATCH"]
    assert report["stage"] == "region_guard"
    assert "secret" not in json.dumps(report)


def test_unconfigured_region_guard_is_backward_compatible_and_provenance_is_safe(monkeypatch):
    gate = gate_module(monkeypatch)
    monkeypatch.delenv("PAPER_VALIDATION_EXPECTED_REGION", raising=False)
    monkeypatch.setenv("APP_SOURCE_REVISION", "postgresql://user:secret@db")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", "secret/region")
    identity = gate.runtime_identity()
    gate.require_expected_region(identity)
    assert identity == {"source_revision": None, "runtime_region": "UNKNOWN", "expected_region": None}


def test_timeframe_matrix_keeps_previous_hosted_scope_until_explicitly_enabled(monkeypatch):
    gate = gate_module(monkeypatch)
    monkeypatch.delenv("PAPER_VALIDATION_TIMEFRAMES", raising=False)
    assert gate.validation_timeframes() == ("15m",)
    monkeypatch.setenv("PAPER_VALIDATION_TIMEFRAMES", "15m,1h,4h")
    assert gate.validation_timeframes() == ("15m", "1h", "4h")


@pytest.mark.parametrize("value", ["", "5m", "15m,15m", "15m, 1h", "15m,1h,4h,15m", "postgresql://secret@host/db"])
async def test_invalid_timeframe_matrix_stops_before_any_hosted_access(monkeypatch, value):
    gate = gate_module(monkeypatch)
    monkeypatch.setenv("PAPER_FULL_FLOW_VALIDATION_ENABLED", "true")
    monkeypatch.delenv("PAPER_VALIDATION_EXPECTED_REGION", raising=False)
    monkeypatch.setenv("PAPER_VALIDATION_TIMEFRAMES", value)
    monkeypatch.setattr(gate, "validate_staging", lambda: pytest.fail("Invalid matrix must stop before staging setup"))
    async def forbidden(*a, **k):
        pytest.fail("Invalid matrix must not reach PostgreSQL")
    monkeypatch.setattr(gate.asyncpg, "connect", forbidden)
    with pytest.raises(ValueError, match="PAPER_VALIDATION_TIMEFRAMES_REFUSED"):
        await gate.main()


@pytest.mark.parametrize("opt_in,tracked,expected", [("false",None,False),("true",None,True),("true","wrong-hash",False)])
async def test_only_explicit_pending_ingest_migration_is_allowed_on_isolated_copy(monkeypatch, tmp_path, opt_in, tracked, expected):
    gate = gate_module(monkeypatch)
    migration = tmp_path / gate.INGEST_LAG_MIGRATION
    migration.write_text("select 1;")
    monkeypatch.setenv("PAPER_VALIDATION_ALLOW_INGEST_LAG_MIGRATION", opt_in)
    async def invariants(connection): return {"tables":55}
    monkeypatch.setattr(gate,"invariants",invariants)
    from types import SimpleNamespace
    monkeypatch.setattr(gate,"Settings",lambda **kwargs:SimpleNamespace(database_pool_size=4,database_max_overflow=0))
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
    class Connection:
        def transaction(self,**kwargs):
            assert kwargs=={"readonly":True}
            return Transaction()
        async def fetch(self,sql):
            assert sql.lstrip().startswith("select")
            return [] if tracked is None else [{"filename":migration.name,"sha256":tracked}]
        async def fetchrow(self,sql):
            assert sql.lstrip().startswith("select")
            return dict(rolconnlimit=8,rolsuper=False,rolcreatedb=False,rolcreaterole=False,rolreplication=False,rolbypassrls=False)
    if expected:
        result=await gate.verify_live_compatibility(Connection(),[migration],SimpleNamespace(database_pool_size=4))
        assert result["pending_isolated_migrations"]==[migration.name]
        assert result["main_database_writes"] is False
    else:
        with pytest.raises(RuntimeError,match="LIVE_MIGRATION_HASH_MISMATCH"):
            await gate.verify_live_compatibility(Connection(),[migration],SimpleNamespace(database_pool_size=4))

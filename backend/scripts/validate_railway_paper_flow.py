"""Gate a PAPER release on isolated PG17 databases on the existing server.

No volume, service or paid resource is provisioned. The original PostgresEU2
host is explicitly refused. Test databases and evidence are retained.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import traceback
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

import asyncpg
import httpx

from app.core.config import Settings
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety
from run_railway_migrations import _quote_ident, _require_private_database_url
from run_railway_staging_backend import validate as validate_staging

DONOR = "capital_cipher_infra_smoke_20261023"
HOST = "capital-cipher-recovered.railway.internal"
PREFIX = "capital_cipher_paper_validation_"


def database_url(url: str, name: str) -> str:
    if not re.fullmatch(PREFIX + r"[a-f0-9]{12}_(schema|flow)", name):
        raise RuntimeError("PAPER_VALIDATION_DATABASE_NAME_REFUSED")
    return urlunsplit(urlsplit(url)._replace(path="/" + name))


@contextmanager
def block_external_http():
    async def blocked_async(_transport, request):
        raise httpx.ConnectError("Isolated PAPER fixture: external HTTP disabled", request=request)

    def blocked_sync(_transport, request):
        raise httpx.ConnectError("Isolated PAPER fixture: external HTTP disabled", request=request)

    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async), patch.object(
        httpx.HTTPTransport, "handle_request", blocked_sync
    ):
        yield


async def fingerprints(connection) -> dict:
    """Compare every existing value, excluding only the four added columns."""
    tables = await connection.fetch(
        "select tablename from pg_tables where schemaname='capital_cipher' order by tablename"
    )
    result = {}
    for row in tables:
        table = row["tablename"]
        excluded = {"paper_orders": ["exit_price", "exit_reason"],
                    "decisions": ["strategy", "warnings"]}.get(table, [])
        digest = await connection.fetchrow(
            "select count(*) as rows, md5(coalesce(string_agg("
            "md5((to_jsonb(t) - $1::text[])::text), '' order by "
            "md5((to_jsonb(t) - $1::text[])::text)), '')) as sha "
            f"from capital_cipher.{_quote_ident(table)} t", excluded,
        )
        result[table] = dict(digest)
    return result


async def invariants(connection) -> dict:
    result = await connection.fetchrow("""
        select
          (select count(*) from pg_tables where schemaname='capital_cipher') as tables,
          (select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='capital_cipher' and c.relkind='r' and c.relrowsecurity) as rls,
          (select count(*) from pg_constraint c join pg_namespace n on n.oid=c.connamespace
            where n.nspname='capital_cipher' and not c.convalidated) as invalid_constraints,
          (select count(*) from pg_index i join pg_class c on c.oid=i.indrelid
            join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='capital_cipher' and not i.indisvalid) as invalid_indexes,
          (select count(*) from pg_trigger t join pg_class c on c.oid=t.tgrelid
            join pg_namespace n on n.oid=c.relnamespace
            where n.nspname='capital_cipher' and not t.tgisinternal and t.tgenabled='D') as disabled_triggers
    """)
    result = dict(result)
    result["constraints"] = await connection.fetchval("""select count(*) from pg_constraint c
        join pg_namespace n on n.oid=c.connamespace where n.nspname='capital_cipher'""")
    result["constraints_sha256"] = hashlib.sha256((await connection.fetchval("""
        select coalesce(string_agg(c.conrelid::regclass::text || ':' || c.conname || ':' ||
            pg_get_constraintdef(c.oid), E'\n' order by c.conrelid::regclass::text, c.conname), '')
        from pg_constraint c join pg_namespace n on n.oid=c.connamespace
        where n.nspname='capital_cipher'""")).encode()).hexdigest()
    assert result["tables"] == result["rls"] == 55, result
    assert not any(result[k] for k in ("invalid_constraints", "invalid_indexes", "disabled_triggers")), result
    return result


async def apply_migrations(connection, migrations) -> None:
    await connection.execute("""create table if not exists public.capital_cipher_schema_migrations (
        filename text primary key, sha256 char(64) not null,
        applied_at timestamptz not null default now())""")
    for migration in migrations:
        statement = migration.read_text(encoding="utf-8")
        digest = hashlib.sha256(statement.encode()).hexdigest()
        existing = await connection.fetchval(
            "select sha256 from public.capital_cipher_schema_migrations where filename=$1", migration.name
        )
        if existing:
            assert existing == digest, "Existing migration hash differs"
            continue
        async with connection.transaction():
            await connection.execute(statement)
            await connection.execute(
                "insert into public.capital_cipher_schema_migrations(filename,sha256) values($1,$2)",
                migration.name, digest,
            )


def validation_settings(name: str, revision: str) -> Settings:
    """Validate the isolated pool before opening a connection or copying a DB.

    Office reserves PostgreSQL capacity and rejects a two-connection pool. With
    the hosted backend capped at four, four validation connections fit the
    existing runtime role limit of eight. No overflow or credential changes.
    """
    office = Settings(_env_file=None, DATABASE_POOL_SIZE=4).office_async_enabled
    return Settings(
        _env_file=None, DATABASE_URL=database_url(os.environ["DATABASE_URL"], name),
        DATABASE_POOL_SIZE=4 if office else 2, DATABASE_MAX_OVERFLOW=0,
        AGENT_WORKER_MAX_CONCURRENCY=2, AGENT_WORKER_BATCH_SIZE=1 if office else 32,
        REDIS_STREAM_PREFIX="cc-paper-validation-" + revision[:12],
        DATA_LAKE_ROOT="/tmp/cc-paper-validation-" + revision[:12], LOG_LEVEL="ERROR",
    )


async def verify_live_compatibility(connection, migrations, settings) -> dict:
    """SELECT-only verification of the live main DB, before isolated writes."""
    async with connection.transaction(readonly=True):
        schema = await invariants(connection)
        tracked = {row["filename"]: row["sha256"] for row in await connection.fetch(
            "select filename, sha256 from public.capital_cipher_schema_migrations")}
        for migration in migrations:
            digest = hashlib.sha256(migration.read_bytes()).hexdigest()
            if tracked.get(migration.name) != digest:
                raise RuntimeError("PAPER_VALIDATION_LIVE_MIGRATION_HASH_MISMATCH")
        runtime = await connection.fetchrow("""select rolconnlimit, rolsuper,
            rolcreatedb, rolcreaterole, rolreplication, rolbypassrls from pg_roles
            where rolname='capital_cipher_app'""")
        backend = Settings(_env_file=None)
        ceiling = backend.database_pool_size + backend.database_max_overflow
        required = ceiling + settings.database_pool_size
        if runtime is None or runtime["rolconnlimit"] < required:
            raise RuntimeError("PAPER_VALIDATION_RUNTIME_CONNECTION_BUDGET_EXCEEDED")
        if any(runtime[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
            raise RuntimeError("PAPER_VALIDATION_RUNTIME_PRIVILEGE_DRIFT")
    return {"schema": schema, "tracked_migrations": len(tracked),
            "backend_connection_ceiling": ceiling,
            "isolated_connection_ceiling": settings.database_pool_size,
            "runtime_role_connection_limit": runtime["rolconnlimit"],
            "main_database_writes": False}


async def main() -> None:
    if os.getenv("PAPER_FULL_FLOW_VALIDATION_ENABLED") != "true":
        raise RuntimeError("PAPER_VALIDATION_EXPLICIT_OPT_IN_REQUIRED")
    validate_staging()
    admin_url = os.environ["MIGRATION_DATABASE_URL"]
    _require_private_database_url(admin_url)
    parsed = urlsplit(admin_url)
    if parsed.hostname != HOST or parsed.path != "/capital_cipher_staging":
        raise RuntimeError("PAPER_VALIDATION_ONLY_RECOVERED_PG17_ALLOWED")
    revision = os.environ.get("APP_SOURCE_REVISION", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RuntimeError("PAPER_VALIDATION_COMMIT_REQUIRED")
    migrations = sorted(Path("/app/migrations").glob("*.sql"))
    if len(migrations) != 15:
        raise RuntimeError("PAPER_VALIDATION_EXPECTS_15_MIGRATIONS")
    names = {kind: PREFIX + revision[:12] + "_" + kind for kind in ("schema", "flow")}
    settings = validation_settings(names["flow"], revision)
    control = await asyncpg.connect(admin_url)
    report = {"event": "CC_PAPER_FULL_FLOW_VALIDATION", "source_revision": revision,
              "execution_environment": "PAPER", "original_volume_modified": False,
              "databases": names, "migrations": len(migrations)}
    try:
        assert int(await control.fetchval("show server_version_num")) // 10000 == 17, "PostgreSQL 17 required"
        report["live_compatibility"] = await verify_live_compatibility(control, migrations, settings)
        donor_size = await control.fetchval("select pg_database_size($1)", DONOR)
        assert donor_size < 400_000_000, "Donor copy exceeds validation storage budget"
        assert not await control.fetchval(
            "select exists(select 1 from pg_stat_activity where datname=$1)", DONOR
        ), "Donor is in use; refusing to interrupt sessions"
        for kind, name in names.items():
            if await control.fetchval("select exists(select 1 from pg_database where datname=$1)", name):
                raise RuntimeError("PAPER_VALIDATION_EXISTING_DATABASE_PRESERVED:" + name)
            template = _quote_ident(DONOR) if kind == "flow" else "template0"
            await control.execute(f"create database {_quote_ident(name)} template {template}")
            marker = "Retained isolated PAPER validation for " + revision
            await control.execute(f"comment on database {_quote_ident(name)} is '{marker}'")
    finally:
        await control.close()

    for kind, name in names.items():
        connection = await asyncpg.connect(database_url(admin_url, name))
        try:
            before = await fingerprints(connection) if kind == "flow" else None
            previous_invariants = await invariants(connection) if kind == "flow" else None
            await apply_migrations(connection, migrations)
            if before is not None:
                after = await fingerprints(connection)
                assert before == after, "Migration modified pre-existing values"
                report["preserved_table_fingerprints"] = after
            report[kind + "_schema"] = await invariants(connection)
            if previous_invariants is not None:
                assert previous_invariants == report[kind + "_schema"], "Existing schema constraints changed"
            print(json.dumps({"event": "CC_PAPER_VALIDATION_SCHEMA", "kind": kind,
                              "migrations": len(migrations), "status": "PASSED"}), flush=True)
            if kind == "flow":
                rows = await connection.fetch("""select p.exit_price, p.exit_reason, a.payload
                    from capital_cipher.paper_orders p join capital_cipher.audit_logs a
                      on a.entity_id=p.id and a.correlation_id=p.correlation_id
                    where p.status='CLOSED' and a.audit_type='PAPER_ORDER_CLOSED'
                      and a.payload->>'paper_order_id'=p.id
                      and a.payload->>'status'='CLOSED'""")
                for row in rows:
                    payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
                    if isinstance(payload.get("exit_price"), (int, float)) and payload["exit_price"] > 0:
                        assert float(row["exit_price"]) == payload["exit_price"]
                    if isinstance(payload.get("exit_reason"), str) and payload["exit_reason"]:
                        assert row["exit_reason"] == payload["exit_reason"]
                report["historical_closures_checked"] = len(rows)
        finally:
            await connection.close()

    # Capacity was checked against the live role before any isolated DB creation.
    with block_external_http():
        report["flows"] = [await validate_full_paper_flow(settings, direction, side)
                           for direction, side in ((1, "BUY"), (-1, "SELL"))]
        report["safety"] = await validate_paper_safety(settings)
    report["status"] = "PASSED"
    print(json.dumps(report, sort_keys=True), flush=True)


def failure_report(exc: Exception) -> dict:
    """Source coordinates diagnose a failed assertion without logging secrets."""
    report = {"event": "CC_PAPER_FULL_FLOW_VALIDATION", "status": "FAILED",
              "error_type": type(exc).__name__,
              "source_locations": [{"file": Path(frame.filename).name,
                                    "function": frame.name, "line": frame.lineno}
                                   for frame in traceback.extract_tb(exc.__traceback__)[-6:]]}
    from app.validation.paper_flow import PaperFlowValidationError
    if isinstance(exc, PaperFlowValidationError):
        report.update(stage=exc.validation_stage, error_codes=exc.validation_error_codes,
                      elapsed_ms=exc.validation_elapsed_ms)
    return report


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Provider URLs/passwords and full SQL parameters must never reach logs.
        print(json.dumps(failure_report(exc)), flush=True)
        raise SystemExit(1) from None

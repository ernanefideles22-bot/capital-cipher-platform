"""Bounded Phase C candidate probe on NEW retained PG17 databases only.

Reuses the existing audit job; does not redeploy the serving backend, mutate
its database, reuse Phase B databases, or contact a venue. Never delete evidence.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from app.core.config import Settings
from app.validation.phase_c_runtime import (
    CRASH_BOUNDARIES, validate_capacity_conflict, validate_concurrent_fault,
    validate_crash_recovery, validate_locked_expiry, validate_three_desks,
)
from run_railway_migrations import _quote_ident, _require_private_database_url
from run_railway_staging_backend import validate as validate_staging
from validate_railway_paper_flow import (
    HOST, apply_migrations, block_external_http, failure_report, invariants,
    require_expected_region, runtime_identity, verify_live_compatibility,
)

PREFIX = "capital_cipher_phase_c_"
KINDS = ("flow", "same", "opposed", "expiry", "faultdb", "faultbroker", "faultaudit", *CRASH_BOUNDARIES)
MAX_TOTAL_DATABASE_BYTES = 200_000_000


def isolated_url(url, name):
    allowed = "|".join(re.escape(kind) for kind in KINDS)
    if not re.fullmatch(PREFIX + r"[a-f0-9]{12}_(?:" + allowed + ")", name):
        raise RuntimeError("PHASE_C_ISOLATED_DATABASE_NAME_REFUSED")
    return urlunsplit(urlsplit(url)._replace(path="/" + name))


def require_scope():
    if os.getenv("PHASE_C_ISOLATED_VALIDATION_ENABLED") != "true":
        raise RuntimeError("PHASE_C_EXPLICIT_OPT_IN_REQUIRED")
    identity = runtime_identity()
    if identity["expected_region"] != "europe-west4-drams3a":
        raise RuntimeError("PHASE_C_REVIEWED_REGION_REQUIRED")
    require_expected_region(identity)
    if identity["source_revision"] is None:
        raise RuntimeError("PHASE_C_EXACT_REVISION_REQUIRED")
    if os.getenv("PAPER_VALIDATION_ALLOW_INGEST_LAG_MIGRATION") != "false":
        raise RuntimeError("PHASE_C_MAIN_MIGRATION_FORBIDDEN")
    validate_staging()
    admin_url = os.environ["MIGRATION_DATABASE_URL"]
    _require_private_database_url(admin_url)
    if urlsplit(admin_url).hostname != HOST or urlsplit(admin_url).path != "/capital_cipher_staging":
        raise RuntimeError("PHASE_C_MAIN_HOST_REFUSED")
    return identity, admin_url


def isolated_settings(name, revision):
    config = Settings(_env_file=None, APP_ENV="local", SYSTEM_MODE="PAPER", OMS_EXECUTION_ENVIRONMENT="PAPER",
        OMS_TESTNET_ENABLED=False, DATABASE_URL=isolated_url(os.environ["DATABASE_URL"], name),
        DATABASE_POOL_SIZE=4, DATABASE_MAX_OVERFLOW=0,
        OFFICE_ASYNC_ENABLED=True, AGENT_WORKER_MAX_CONCURRENCY=2, AGENT_WORKER_BATCH_SIZE=1,
        REDIS_STREAM_PREFIX="cc-phase-c-" + revision[:12] + "-" + name.removeprefix(PREFIX + revision[:12] + "_"),
        DATA_LAKE_ROOT="/tmp/cc-phase-c-" + revision[:12] + "/" + name,
        ENABLE_MARKET_DATA=False, OPERATIONS_MONITOR_ENABLED=False, BACKFILL_WORKER_ENABLED=False,
        MAX_SINGLE_POSITION_PERCENT=20, MAX_STRATEGY_EXPOSURE_PERCENT=20, LOG_LEVEL="ERROR")
    if (config.office_primary_timeout_seconds, config.office_shadow_timeout_seconds,
            config.office_shadow_ttl_seconds) != (3, 5, 120):
        raise RuntimeError("PHASE_C_REVIEWED_DEADLINES_REQUIRED")
    return config


async def retained_sizes(control, names):
    sizes = {kind: await control.fetchval("select pg_database_size($1)", name) for kind, name in names.items()}
    if sum(sizes.values()) > MAX_TOTAL_DATABASE_BYTES:
        raise RuntimeError("PHASE_C_STORAGE_BUDGET_EXCEEDED")
    return sizes


async def main():
    identity, admin_url = require_scope()
    revision = identity["source_revision"]
    names = {kind: PREFIX + revision[:12] + "_" + kind for kind in KINDS}
    configs = {kind: isolated_settings(name, revision) for kind, name in names.items()}
    migrations = sorted(Path("/app/migrations").glob("*.sql"))
    if len(migrations) != 16:
        raise RuntimeError("PHASE_C_EXPECTS_16_MIGRATIONS")
    report = {"event": "CC_PHASE_C_ISOLATED_VALIDATION", **identity, "databases": names,
        "execution_environment": "PAPER", "live_execution_attempted": False,
        "main_database_writes": False, "databases_retained": True,
        "storage_budget_bytes": MAX_TOTAL_DATABASE_BYTES,
        "scope": "candidate synthetic acceptance; not serving runtime or financial certification"}
    control = await asyncpg.connect(admin_url)
    try:
        if int(await control.fetchval("show server_version_num")) // 10000 != 17:
            raise RuntimeError("PHASE_C_POSTGRES17_REQUIRED")
        total = await control.fetchval("select sum(pg_database_size(oid)) from pg_database")
        if int(total) + MAX_TOTAL_DATABASE_BYTES > 3_800_000_000:
            raise RuntimeError("PHASE_C_CLUSTER_HEADROOM_REQUIRED")
        report["live_compatibility"] = await verify_live_compatibility(control, migrations, configs["flow"])
        for name in names.values():
            if await control.fetchval("select exists(select 1 from pg_database where datname=$1)", name):
                raise RuntimeError("PHASE_C_EXISTING_DATABASE_PRESERVED")
        for name in names.values():
            await control.execute(f"create database {_quote_ident(name)} template template0")
            await control.execute(f"comment on database {_quote_ident(name)} is 'Retained Phase C isolated PAPER validation {revision}'")
            connection = await asyncpg.connect(isolated_url(admin_url, name))
            try:
                await apply_migrations(connection, migrations)
                await invariants(connection)
            finally:
                await connection.close()
        report["checks"] = []
        async def checkpoint(kind, awaitable):
            result = await awaitable
            report["checks"].append({"kind": kind, "result": result})
            await retained_sizes(control, names)
            print(json.dumps({"event": "CC_PHASE_C_CHECK", "kind": kind, "status": "PASSED"}), flush=True)
        with block_external_http():
            await checkpoint("flow", validate_three_desks(configs["flow"]))
            for opposing, kind in ((False, "same"), (True, "opposed")):
                await checkpoint(kind, validate_capacity_conflict(configs[kind], opposing))
            await checkpoint("expiry", validate_locked_expiry(configs["expiry"]))
            for fault, kind in (("database", "faultdb"), ("broker", "faultbroker"), ("audit", "faultaudit")):
                await checkpoint(kind, validate_concurrent_fault(configs[kind], fault))
            for boundary in CRASH_BOUNDARIES:
                await checkpoint(boundary, validate_crash_recovery(configs[boundary], boundary, "/tmp/cc-phase-c-crash-"+revision[:12]))
        report["retained_database_sizes_bytes"] = await retained_sizes(control, names)
        report["status"] = "PASSED"
        print(json.dumps(report, sort_keys=True), flush=True)
    finally:
        await control.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        report = failure_report(exc)
        report["event"] = "CC_PHASE_C_ISOLATED_VALIDATION"
        print(json.dumps(report), flush=True)
        raise SystemExit(1) from None

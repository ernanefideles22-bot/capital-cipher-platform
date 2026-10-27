"""Final regional PAPER probes in new empty PG17 databases, with retained evidence.

No donor clone, deletion, migration of the main database, or live venue call.
Run only after the reviewed topology and backend publication have settled.
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
from app.validation.office_runtime_certification import certify_cpu_isolation, certify_dependency_fault
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety
from run_railway_migrations import _quote_ident, _require_private_database_url
from run_railway_staging_backend import validate as validate_staging
from validate_railway_paper_flow import (
    HOST, apply_migrations, block_external_http, failure_report, invariants,
    require_expected_region, runtime_identity, verify_live_compatibility,
)

PREFIX = "capital_cipher_final_b_"
KINDS = ("flow", "cpu15m", "cpu1h", "cpu4h", "faultdb", "faultbroker", "faultaudit")
MAX_TOTAL_DATABASE_BYTES = 160_000_000


def isolated_url(url, name):
    if not re.fullmatch(PREFIX + r"[a-f0-9]{12}_(flow|cpu15m|cpu1h|cpu4h|faultdb|faultbroker|faultaudit)", name):
        raise RuntimeError("CERTIFICATION_DATABASE_NAME_REFUSED")
    return urlunsplit(urlsplit(url)._replace(path="/" + name))


def require_scope():
    if os.getenv("PHASE_B_FINAL_CERTIFICATION_ENABLED") != "true":
        raise RuntimeError("CERTIFICATION_EXPLICIT_OPT_IN_REQUIRED")
    identity = runtime_identity()
    if identity["expected_region"] != "europe-west4-drams3a":
        raise RuntimeError("CERTIFICATION_REVIEWED_REGION_REQUIRED")
    require_expected_region(identity)
    if identity["source_revision"] is None:
        raise RuntimeError("CERTIFICATION_COMMIT_REQUIRED")
    if os.getenv("PAPER_VALIDATION_ALLOW_INGEST_LAG_MIGRATION") != "false":
        raise RuntimeError("CERTIFICATION_MAIN_MIGRATION_MUST_BE_COMPLETE")
    validate_staging()
    admin_url = os.environ["MIGRATION_DATABASE_URL"]
    _require_private_database_url(admin_url)
    if urlsplit(admin_url).hostname != HOST or urlsplit(admin_url).path != "/capital_cipher_staging":
        raise RuntimeError("CERTIFICATION_MAIN_HOST_REFUSED")
    return identity, admin_url


def isolated_settings(name, revision):
    settings = Settings(_env_file=None,
        DATABASE_URL=isolated_url(os.environ["DATABASE_URL"], name),
        DATABASE_POOL_SIZE=4, DATABASE_MAX_OVERFLOW=0,
        OFFICE_ASYNC_ENABLED=True, AGENT_WORKER_MAX_CONCURRENCY=2, AGENT_WORKER_BATCH_SIZE=1,
        REDIS_STREAM_PREFIX="cc-final-b-" + revision[:12] + "-" + name.rsplit("_", 1)[-1],
        DATA_LAKE_ROOT="/tmp/cc-final-b-" + revision[:12] + "/" + name,
        LOG_LEVEL="ERROR")
    if (settings.office_primary_timeout_seconds, settings.office_shadow_timeout_seconds,
            settings.office_shadow_ttl_seconds) != (3, 5, 120):
        raise RuntimeError("CERTIFICATION_REVIEWED_DEADLINES_REQUIRED")
    return settings


async def retained_sizes(control, names):
    sizes = {kind: await control.fetchval("select pg_database_size($1)", name)
             for kind, name in names.items()}
    if sum(sizes.values()) > MAX_TOTAL_DATABASE_BYTES:
        raise RuntimeError("CERTIFICATION_RETAINED_STORAGE_BUDGET_EXCEEDED")
    return sizes


async def main():
    identity, admin_url = require_scope()
    revision = identity["source_revision"]
    names = {kind: PREFIX + revision[:12] + "_" + kind for kind in KINDS}
    settings = {kind: isolated_settings(name, revision) for kind, name in names.items()}
    migrations = sorted(Path("/app/migrations").glob("*.sql"))
    if len(migrations) != 16:
        raise RuntimeError("CERTIFICATION_EXPECTS_16_MIGRATIONS")
    report = {"event": "CC_PHASE_B_FINAL_CERTIFICATION", **identity,
        "databases": names, "execution_environment": "PAPER", "live_execution_attempted": False,
        "main_database_writes": False, "databases_retained": True,
        "storage_budget_bytes": MAX_TOTAL_DATABASE_BYTES,
        "measurement_scope": "synthetic isolated job in the final region; not a production SLO",
        "restarts": "application rebuilds; container replacement verified separately on the backend"}
    control = await asyncpg.connect(admin_url)
    try:
        if int(await control.fetchval("show server_version_num")) // 10000 != 17:
            raise RuntimeError("CERTIFICATION_POSTGRES17_REQUIRED")
        report["live_compatibility"] = await verify_live_compatibility(control, migrations, settings["flow"])
        # Check every name before the first CREATE, preserving all earlier test copies.
        for name in names.values():
            if await control.fetchval("select exists(select 1 from pg_database where datname=$1)", name):
                raise RuntimeError("CERTIFICATION_EXISTING_DATABASE_PRESERVED")
        for kind, name in names.items():
            await control.execute(f"create database {_quote_ident(name)} template template0")
            await control.execute(f"comment on database {_quote_ident(name)} is 'Retained final PAPER certification {revision}'")
            connection = await asyncpg.connect(isolated_url(admin_url, name))
            try:
                await apply_migrations(connection, migrations)
                await invariants(connection)
            finally:
                await connection.close()
        report["flows"] = []
        report["cpu_isolation"] = []
        report["dependency_faults"] = []
        with block_external_http():
            for timeframe in ("15m", "1h", "4h"):
                for direction, side in ((1, "BUY"), (-1, "SELL")):
                    result = await validate_full_paper_flow(settings["flow"], direction, side, timeframe=timeframe)
                    report["flows"].append(result)
                    await retained_sizes(control, names)
                    print(json.dumps({"event": "CC_FINAL_B_FLOW", "timeframe": timeframe,
                        "side": side, "status": "PASSED", "decision_elapsed_ms": result["decision_elapsed_ms"]}), flush=True)
            for timeframe in ("15m", "1h", "4h"):
                report["cpu_isolation"].append(await certify_cpu_isolation(
                    settings["cpu" + timeframe], timeframe, "/tmp/cc-final-b-markers-" + revision[:12]))
                await retained_sizes(control, names)
            for fault, kind in (("database", "faultdb"), ("broker", "faultbroker"), ("audit", "faultaudit")):
                report["dependency_faults"].append(await certify_dependency_fault(settings[kind], fault))
                await retained_sizes(control, names)
            report["safety"] = await validate_paper_safety(settings["flow"])
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
        report["event"] = "CC_PHASE_B_FINAL_CERTIFICATION"
        print(json.dumps(report), flush=True)
        raise SystemExit(1) from None

"""Read-only predeploy gate after an independently validated Supabase restore."""

from __future__ import annotations

import asyncio
import json
import os

from sqlalchemy import text

from app.core.config import Settings
from app.database.session import Database
from app.operations.railway_supabase import prepare_ca, validate_supabase_database
from scripts.run_railway_staging_backend import validate


async def main() -> None:
    if os.getenv("STAGING_DATABASE_PROVIDER") != "supabase":
        raise RuntimeError("SUPABASE_PROVIDER_REQUIRED")
    validate()  # Also enforces the PAPER, TESTNET and Redis boundaries.
    settings = Settings(_env_file=None)
    prepare_ca(os.environ)
    validate_supabase_database(settings, os.environ)
    database = Database(settings.database_url, pool_size=1, max_overflow=0)
    try:
        async with database.engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                role = (await connection.execute(text("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit FROM pg_roles WHERE rolname=current_user"))).mappings().one()
                if (role['rolname'] != 'capital_cipher_staging'
                    or any(role[k] for k in ('rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls'))
                    or role['rolconnlimit'] != 8):
                    raise RuntimeError("SUPABASE_RUNTIME_ROLE_INVALID")
                if not await connection.scalar(text("SELECT pg_has_role(current_user, 'capital_cipher_runtime', 'member')")):
                    raise RuntimeError("SUPABASE_RUNTIME_ROLE_MEMBERSHIP_MISSING")
                if not await connection.scalar(text("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")):
                    raise RuntimeError("SUPABASE_TLS_NOT_ACTIVE")
        await database.verify_schema()
        print(json.dumps({'status':'PASS','mode':'PAPER','checks':['verified_tls','runtime_role','schema','rls'],'data_or_schema_modified':False}))
    finally:
        await database.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=45))
    except Exception as error:
        # Connection exceptions can contain a DSN. Emit only a safe error type.
        raise SystemExit(f"SUPABASE_PREDEPLOY_FAILED:{type(error).__name__}") from None

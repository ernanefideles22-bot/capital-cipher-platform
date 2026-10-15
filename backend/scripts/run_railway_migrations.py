"""Apply versioned Capital Cipher migrations to Railway staging safely."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg

PRIVATE_SUFFIX = ".railway.internal"
RUNTIME_ROLE = "capital_cipher_app"
GROUP_ROLE = "capital_cipher_runtime"
LOCK_KEY = 746218905


def _require_private_database_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    expected = os.getenv("STAGING_EXPECTED_DATABASE_HOST", "").strip().lower()
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise RuntimeError("RAILWAY_MIGRATION_DATABASE_SCHEME_INVALID")
    if not expected or host != expected or not host.endswith(PRIVATE_SUFFIX):
        raise RuntimeError("RAILWAY_MIGRATION_DATABASE_HOST_INVALID")
    if (parsed.port or 5432) != 5432:
        raise RuntimeError("RAILWAY_MIGRATION_DATABASE_PORT_INVALID")


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def main() -> None:
    database_url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    runtime_password = os.getenv("RUNTIME_DATABASE_PASSWORD", "")
    if len(runtime_password) < 32 or len(set(runtime_password)) < 8:
        raise RuntimeError("RAILWAY_RUNTIME_DATABASE_PASSWORD_WEAK")
    _require_private_database_url(database_url)

    migrations_dir = Path("/app/migrations")
    migrations = sorted(migrations_dir.glob("*.sql"))
    if not migrations:
        raise RuntimeError("RAILWAY_MIGRATIONS_NOT_FOUND")

    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute("select pg_advisory_lock($1)", LOCK_KEY)
        await connection.execute(
            """
            create table if not exists public.capital_cipher_schema_migrations (
                filename text primary key,
                sha256 char(64) not null,
                applied_at timestamptz not null default now()
            )
            """
        )

        applied_count = 0
        for migration in migrations:
            statement = migration.read_text(encoding="utf-8")
            digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()
            existing = await connection.fetchrow(
                "select sha256 from public.capital_cipher_schema_migrations where filename = $1",
                migration.name,
            )
            if existing:
                if existing["sha256"] != digest:
                    raise RuntimeError(
                        f"RAILWAY_MIGRATION_HASH_MISMATCH:{migration.name}"
                    )
                continue
            async with connection.transaction():
                await connection.execute(statement)
                await connection.execute(
                    "insert into public.capital_cipher_schema_migrations(filename, sha256) values ($1, $2)",
                    migration.name,
                    digest,
                )
            applied_count += 1

        group_exists = await connection.fetchval(
            "select exists(select 1 from pg_roles where rolname = $1)", GROUP_ROLE
        )
        if not group_exists:
            raise RuntimeError("RAILWAY_RUNTIME_GROUP_ROLE_MISSING_AFTER_MIGRATIONS")

        quoted_password = await connection.fetchval("select quote_literal($1)", runtime_password)
        runtime_exists = await connection.fetchval(
            "select exists(select 1 from pg_roles where rolname = $1)", RUNTIME_ROLE
        )
        role_ident = _quote_ident(RUNTIME_ROLE)
        group_ident = _quote_ident(GROUP_ROLE)
        if runtime_exists:
            await connection.execute(
                f"alter role {role_ident} login password {quoted_password}"
            )
        else:
            await connection.execute(
                f"create role {role_ident} login password {quoted_password}"
            )
        await connection.execute(f"grant {group_ident} to {role_ident}")
        await connection.execute(
            f"alter role {role_ident} set search_path = capital_cipher, public"
        )

        print(
            f"Railway migrations ready: {len(migrations)} tracked, {applied_count} newly applied",
            flush=True,
        )
    finally:
        try:
            await connection.execute("select pg_advisory_unlock($1)", LOCK_KEY)
        finally:
            await connection.close()


if __name__ == "__main__":
    asyncio.run(main())

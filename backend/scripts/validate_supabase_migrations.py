"""Apply every Supabase migration to an empty, local disposable database."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

ACKNOWLEDGEMENT = "EPHEMERAL_TEST_DATABASE"


async def main() -> None:
    database_url = os.environ.get("POSTGRES_MIGRATION_VALIDATION_URL", "")
    acknowledgement = os.environ.get("MIGRATION_VALIDATION_ACK", "")
    if acknowledgement != ACKNOWLEDGEMENT:
        raise RuntimeError("Migration validation acknowledgement is missing")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise RuntimeError("Migration validation requires a PostgreSQL URL")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            "Migration validation is restricted to a local disposable database"
        )
    repository_root = Path(__file__).resolve().parents[2]
    migrations = sorted(
        (repository_root / "supabase" / "migrations").glob("*.sql")
    )
    if not migrations:
        raise RuntimeError("No Supabase migrations were found")
    connection = await asyncpg.connect(database_url)
    disposable_schema_owned = False
    try:
        existing = await connection.fetchval(
            "select to_regnamespace('capital_cipher') is not null"
        )
        if existing:
            raise RuntimeError(
                "Migration validation refuses a non-empty platform schema"
            )
        disposable_schema_owned = True
        for migration in migrations:
            statement = migration.read_text(encoding="utf-8")
            async with connection.transaction():
                await connection.execute(statement)
        tables = await connection.fetchval(
            """
            select count(*)
            from information_schema.tables
            where table_schema = 'capital_cipher'
            """
        )
        if tables < 1:
            raise RuntimeError("Migrations created no private tables")
        print(
            f"Validated {len(migrations)} migrations and {tables} private tables"
        )
    finally:
        if disposable_schema_owned:
            await connection.execute(
                "drop schema if exists capital_cipher cascade"
            )
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())

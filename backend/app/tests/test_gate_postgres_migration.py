"""Validate Gate venue migration against disposable local PostgreSQL only."""
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_gate_migration_preserves_existing_evidence_and_rejects_other_venues():
    import asyncpg
    url=urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://","postgresql://"))
    assert url.hostname in {"localhost","127.0.0.1","::1"}, "Refusing non-local database"
    name="gate_migration_test_"+uuid4().hex[:12]
    admin=await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await admin.fetchval("show server_version_num")) // 10000 == 17
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    finally:
        await admin.close()
    connection=await asyncpg.connect(urlunsplit(url._replace(path="/"+name)))
    try:
        migrations=sorted((Path(__file__).resolve().parents[3]/"supabase/migrations").glob("*.sql"))
        target=next(m for m in migrations if m.name=="20261029000000_add_gate_oms_venue.sql")
        for migration in (m for m in migrations if m.name < target.name):
            async with connection.transaction():
                await connection.execute(migration.read_text())
        insert="""INSERT INTO capital_cipher.reconciliation_runs
            (run_id,exchange,environment,status,local_order_count,venue_order_count,fill_count,
             position_count,balance_count,mismatch_count,critical_mismatch_count,started_at,completed_at)
            VALUES ($1,$2,'TESTNET','MATCHED',0,0,0,0,0,0,0,now(),now())"""
        await connection.execute(insert,"historic-bybit","BYBIT")
        before=await connection.fetchrow("SELECT * FROM capital_cipher.reconciliation_runs WHERE run_id='historic-bybit'")
        security=await connection.fetchrow("""SELECT relowner,relrowsecurity,relforcerowsecurity,relacl::text
            FROM pg_class WHERE oid='capital_cipher.reconciliation_runs'::regclass""")
        async with connection.transaction():
            await connection.execute(target.read_text())
        assert await connection.fetchrow("SELECT * FROM capital_cipher.reconciliation_runs WHERE run_id='historic-bybit'")==before
        assert await connection.fetchrow("""SELECT relowner,relrowsecurity,relforcerowsecurity,relacl::text
            FROM pg_class WHERE oid='capital_cipher.reconciliation_runs'::regclass""")==security
        await connection.execute(insert,"new-gate","GATE")
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(insert,"bad-venue","UNKNOWN")
        names=["ck_oms_order_exchange","ck_execution_fill_exchange","ck_reconciliation_run_exchange",
               "ck_reconciliation_mismatch_exchange","ck_venue_position_exchange","ck_venue_balance_exchange"]
        rows=await connection.fetch("""SELECT conname,pg_get_constraintdef(oid) AS definition,convalidated
            FROM pg_constraint WHERE conname=ANY($1::text[])""",names)
        assert len(rows)==len(names) and all(row["convalidated"] and "GATE" in row["definition"] for row in rows)
    finally:
        await connection.close()
        cleanup=await asyncpg.connect(urlunsplit(url))
        try:
            await cleanup.execute(f'DROP DATABASE "{name}"')
        finally:
            await cleanup.close()

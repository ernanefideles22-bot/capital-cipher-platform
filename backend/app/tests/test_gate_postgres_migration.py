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


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
async def test_gate_oms_entry_requires_durable_stop_and_target():
    import asyncpg
    import httpx
    from app.audit.service import AuditService
    from app.core.state_machine import SystemState, SystemStateMachine
    from app.database.repositories.repository import Repository
    from app.database.session import Database
    from app.execution.adapters.gate_testnet_metadata import GateTestnetMetadata
    from app.execution.adapters.gate_testnet_preparation import GateTestnetPreparationAdapter
    from app.oms.service import OMSService
    from app.paper_trading.engine import PaperTradingEngine
    from app.risk.venue_aware import VenueAwareRiskManager
    from app.schemas.common import CandidateAction, Exchange
    from app.schemas.oms import ExecutionEnvironment
    from app.schemas.risk import RiskLimits
    from app.tests.conftest import make_decision

    url=urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://","postgresql://"))
    assert url.hostname in {"localhost","127.0.0.1","::1"}, "Refusing non-local database"
    name="gate_protection_test_"+uuid4().hex[:12]
    admin=await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await admin.fetchval("show server_version_num")) // 10000 == 17
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    finally:
        await admin.close()
    target=urlunsplit(url._replace(path="/"+name))
    connection=await asyncpg.connect(target)
    database=None
    provider=None
    try:
        migrations=sorted((Path(__file__).resolve().parents[3]/"supabase/migrations").glob("*.sql"))
        for migration in migrations:
            async with connection.transaction():
                await connection.execute(migration.read_text())
        database=Database(target.replace("postgresql://","postgresql+asyncpg://"),pool_size=2)
        repository=Repository(database)
        sm=SystemStateMachine()
        await sm.transition(SystemState.INITIALIZING,reason="test",actor="test")
        await sm.transition(SystemState.PAPER,reason="test",actor="test")
        audit=AuditService(repository=repository)
        risk=VenueAwareRiskManager(RiskLimits(),sm,audit,repository=repository,
            execution_environment=ExecutionEnvironment.TESTNET,testnet_exchange=Exchange.GATE)
        async def equity(): return 10000.0
        risk.set_testnet_equity_provider(equity)
        await risk.initialize()
        paper=PaperTradingEngine(audit,risk,repository=repository)
        def handler(request):
            assert request.method=="GET" and request.url.host=="api-testnet.gateapi.io"
            return httpx.Response(200,json={"name":"BTC_USDT","quanto_multiplier":"0.0001",
                "order_size_min":"1","order_size_max":"100000","in_delisting":False})
        provider=GateTestnetMetadata(transport=httpx.MockTransport(handler),clock=lambda:1000)
        adapter=GateTestnetPreparationAdapter(provider,clock=lambda:1001)
        async def release(): return True
        oms=OMSService(target_environment=ExecutionEnvironment.TESTNET,target_exchange=Exchange.GATE,
            paper_engine=paper,risk_manager=risk,audit_service=audit,repository=repository,release_guard=release,
            adapters={(Exchange.GATE,ExecutionEnvironment.TESTNET):adapter})
        decision=make_decision(CandidateAction.BUY)
        check=await risk.check(decision,entry_price=100,atr=1,leverage=1)
        assert check.approved
        order=await oms.submit_approved(decision,check,current_price=100)
        protected=await connection.fetchrow("""SELECT exchange,environment,stop_loss,take_profit,leverage,approval_id
            FROM capital_cipher.oms_order_protections WHERE oms_order_id=$1""",order.oms_order_id)
        assert protected["exchange"]=="GATE" and protected["environment"]=="TESTNET"
        assert float(protected["stop_loss"])==check.stop_loss and float(protected["take_profit"])==check.take_profit
        assert float(protected["leverage"])==1 and protected["approval_id"]==check.approval_id
        with pytest.raises(asyncpg.ObjectNotInPrerequisiteStateError, match="protection evidence is immutable"):
            await connection.execute("UPDATE capital_cipher.oms_order_protections SET stop_loss=1 WHERE oms_order_id=$1",order.oms_order_id)
        assert not await adapter.healthcheck()
    finally:
        if provider is not None: await provider.aclose()
        if database is not None: await database.dispose()
        await connection.close()
        cleanup=await asyncpg.connect(urlunsplit(url))
        try:
            await cleanup.execute(f'DROP DATABASE "{name}"')
        finally:
            await cleanup.close()

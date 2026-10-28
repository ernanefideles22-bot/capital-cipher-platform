"""Three independent PAPER desks contend for the same durable PG17 capacity.

Only a new localhost CI database is allowed; no hosted database or exchange.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from app.core.errors import DatabaseError
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.schemas.common import CandidateAction
from app.schemas.paper import PaperOrder
from app.schemas.risk import RiskLimits
from app.tests.conftest import make_decision
from app.tests.test_month_6_central_risk import risk_stack


@pytest.mark.skipif(not os.environ.get("POSTGRES_TEST_URL"), reason="POSTGRES_TEST_URL is not configured")
@pytest.mark.parametrize("opposing", [False, True])
async def test_three_desks_share_one_durable_capacity(opposing):
    import asyncpg

    url = urlsplit(os.environ["POSTGRES_TEST_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    assert url.hostname in {"localhost", "127.0.0.1", "::1"}, "Only disposable localhost CI is allowed"
    name = "capital_cipher_phase_c_" + uuid4().hex[:12]
    connection = await asyncpg.connect(urlunsplit(url))
    try:
        assert int(await connection.fetchval("show server_version_num")) // 10000 == 17
        await connection.execute(f'create database "{name}" template template0')
    finally:
        await connection.close()
    target = urlunsplit(url._replace(path="/" + name))
    connection = await asyncpg.connect(target)
    try:
        for migration in sorted((Path(__file__).resolve().parents[3] / "supabase/migrations").glob("*.sql")):
            await connection.execute(migration.read_text())
        assert await connection.fetchval("select count(*) from capital_cipher.risk_control_state where singleton_id=1") == 1
    finally:
        await connection.close()

    databases = [Database(target.replace("postgresql://", "postgresql+asyncpg://"), pool_size=1) for _ in range(3)]
    repositories = [Repository(database) for database in databases]
    # A single desk can fill 40% of 10,000. All three independent approvals
    # initially see an empty portfolio, but together they may not fill 120%.
    limits = RiskLimits(max_gross_exposure_percent=40, max_net_exposure_percent=40,
                        max_symbol_exposure_percent=40)
    ready, arrived = asyncio.Event(), 0
    plans = []
    try:
        for repository, (timeframe, strategy) in zip(repositories, (
            ("15m", "SCALP_15M_v1"), ("1h", "DAY_1H_v1"), ("4h", "SWING_4H_v1")
        )):
            _, _, manager, engine = await risk_stack(limits=limits, repository=repository)
            await manager.initialize()
            decision = make_decision(action=CandidateAction.SELL if opposing and timeframe == "1h" else CandidateAction.BUY)
            decision = decision.model_copy(update={"timeframe": timeframe, "strategy": strategy})
            await repository.save_decision(decision)
            check = await manager.check(decision, entry_price=100, atr=1, balance=10_000)
            assert check.approved and check.position_size == 4000
            consume = repository.consume_order_approval

            async def simultaneous_consume(*args, _consume=consume, **kwargs):
                nonlocal arrived
                arrived += 1
                if arrived == 3:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 10)
                return await _consume(*args, **kwargs)

            repository.consume_order_approval = simultaneous_consume
            plans.append((engine, decision, check))

        results = await asyncio.wait_for(asyncio.gather(*(
            engine.create_order(decision, check, current_price=100)
            for engine, decision, check in plans
        ), return_exceptions=True), 20)
        assert arrived == 3, "All three distinct approvals must reach consumption together"
        accepted = [result for result in results if isinstance(result, PaperOrder)]
        rejected = [result for result in results if isinstance(result, Exception)]
        assert len(accepted) == 1
        assert len(rejected) == 2
        assert all(isinstance(error, DatabaseError) and "stale for the durable portfolio" in str(error) for error in rejected)
        positions = await repositories[0].load_open_position_exposures()
        assert len(positions) == 1
        assert sum(position.notional for position in positions) == 4000
        assert positions[0].strategy in {"SCALP_15M_v1", "DAY_1H_v1", "SWING_4H_v1"}

        _, _, restarted, _ = await risk_stack(limits=limits, repository=repositories[1])
        await restarted.initialize()
        assert len(restarted.position_exposures()) == 1
        assert restarted.position_exposures()[0].notional == 4000
        print({"opposing": opposing, "distinct_desks": 3, "accepted": 1,
               "stale_rejected": 2, "gross": 4000, "restart_restored": True})
    finally:
        for database in databases:
            await database.dispose()
    # Database/evidence are retained until the disposable CI service ends.

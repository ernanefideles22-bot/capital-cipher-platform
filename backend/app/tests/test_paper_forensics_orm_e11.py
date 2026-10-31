"""E1.1 regression: SQLAlchemy ORM read must materialize entity objects.

An AsyncConnection.execute(select(ORMClass)) produces Core row columns, and
.scalars() returns the first scalar (paper_order ID), NOT ORM instances. This
historically broke the diagnostic exporter despite its pure analysis tests.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database.models import INTERNAL_SCHEMA, PaperOrderModel


@pytest.mark.asyncio
async def test_readonly_export_uses_bound_orm_session_to_read_paper_orders():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={
            "schema_translate_map": {INTERNAL_SCHEMA: None},
        },
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(PaperOrderModel.__table__.create)
            # Fixture seeding belongs only in this disposable SQLite DB.
            await connection.execute(
                PaperOrderModel.__table__.insert().values(
                    id="order-1",
                    decision_id="decision-1",
                    risk_check_id="risk-1",
                    correlation_id="corr-1",
                    exchange="BINANCE",
                    symbol="BTCUSDT",
                    timeframe="15m",
                    strategy="SCALP_15M_V1",
                    side="BUY",
                    entry_price=100,
                    stop_loss=98,
                    take_profit=104,
                    position_size=1000,
                    leverage=1,
                    status="CLOSED",
                    fees_estimated=1,
                    opened_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
                    closed_at=datetime(2026, 10, 30, 1, tzinfo=timezone.utc),
                    pnl=-10,
                    created_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
                )
            )
        async with engine.connect() as conn:
            # The production exporter runs SET TRANSACTION READ ONLY before
            # constructing its bound ORM session (PostgreSQL only).
            async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                entities = list(
                    (await session.execute(select(PaperOrderModel))).scalars()
                )
                assert len(entities) == 1
                assert isinstance(entities[0], PaperOrderModel)
                assert entities[0].decision_id == "decision-1"
                assert float(entities[0].pnl) == -10.0
            raw = list((await conn.execute(select(PaperOrderModel))).scalars())
            assert raw == ["order-1"]  # The original E1 bug.
    finally:
        await engine.dispose()

"""Exercise the actual SQL read path with the decimal observed on Gate staging.

SQLite TEXT preserves the exact decimal text and a dedicated converter preserves
the PostgreSQL boolean type. Separate PG17 tests exercise the real NUMERIC driver.
"""
import math
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.errors import SecurityError
from app.execution.gate_protection_store import GateProtectionStore
from app.execution.gate_protection_validation import validate_gate_protection
from app.tests.test_gate_protection_validation import order, evidence

RAW_REFERENCE = "77165.429999999993015081"


@pytest.fixture
async def persisted():
    sqlite3.register_converter("GATE_TEST_BOOL", lambda value: value == b"1")
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"detect_types": sqlite3.PARSE_DECLTYPES},
    )
    item = order().model_copy(update={"reference_price": 77165.43})
    row = evidence() | {
        "reference_price": RAW_REFERENCE,
        "stop_loss": "76666.900000000000000000",
        "take_profit": "78162.490000000000000000",
    }
    async with engine.begin() as conn:
        await conn.execute(text("ATTACH DATABASE ':memory:' AS capital_cipher"))
        await conn.execute(text(
            "CREATE TABLE capital_cipher.oms_orders "
            "(oms_order_id TEXT, approval_id TEXT, reference_price TEXT, leverage TEXT)"
        ))
        columns = ",".join(
            f"{key} {'GATE_TEST_BOOL' if key == 'reduce_only' else 'TEXT'}"
            for key in row
        )
        await conn.execute(text(
            f"CREATE TABLE capital_cipher.oms_order_protections ({columns})"
        ))
        await conn.execute(text(
            "INSERT INTO capital_cipher.oms_orders VALUES "
            "(:oms_order_id,:approval_id,:reference_price,:leverage)"
        ), row)
        await conn.execute(text(
            f"INSERT INTO capital_cipher.oms_order_protections ({','.join(row)}) "
            f"VALUES ({','.join(':'+key for key in row)})"
        ), row)
    try:
        yield engine, item
    finally:
        await engine.dispose()
        sqlite3.converters.pop("GATE_TEST_BOOL", None)


@pytest.mark.asyncio
async def test_observed_postgres_decimal_projection_is_accepted_without_price_tolerance(persisted):
    engine, item = persisted
    async with engine.connect() as conn:
        raw = (await conn.execute(text(
            "SELECT * FROM capital_cipher.oms_order_protections"
        ))).mappings().one()
        assert raw["reduce_only"] is False
        # The original staging failure, before database float projection.
        with pytest.raises(SecurityError):
            validate_gate_protection(item, raw)
    result = await GateProtectionStore(SimpleNamespace(engine=engine)).verify(item)
    assert result["verified"] is True and result["execution_authorized"] is False


@pytest.mark.asyncio
async def test_distinct_stored_references_that_round_to_same_float_still_block(persisted):
    engine, item = persisted
    changed = "77165.429999999993115081"
    assert changed != RAW_REFERENCE and float(changed) == float(RAW_REFERENCE)
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE capital_cipher.oms_order_protections SET reference_price=:value"
        ), {"value": changed})
    with pytest.raises(SecurityError):
        await GateProtectionStore(SimpleNamespace(engine=engine)).verify(item)


@pytest.mark.asyncio
async def test_different_model_reference_still_blocks(persisted):
    engine, item = persisted
    changed = item.model_copy(update={
        "reference_price": math.nextafter(item.reference_price, math.inf)
    })
    with pytest.raises(SecurityError):
        await GateProtectionStore(SimpleNamespace(engine=engine)).verify(changed)

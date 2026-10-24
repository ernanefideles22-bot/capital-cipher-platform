"""Full PAPER flow uses the same harness as the isolated PostgreSQL gate."""
from __future__ import annotations

import pytest
from app.core.config import Settings
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety


@pytest.mark.parametrize("direction,expected_side", [(1, "BUY"), (-1, "SELL")])
async def test_full_application_paper_flow_and_restart(tmp_path, direction, expected_side):
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'paper-flow.db'}",
        REDIS_URL=None, AGENT_WORKER_ENABLED=False, BACKFILL_WORKER_ENABLED=False,
        ENABLE_MARKET_DATA=False, OPERATIONS_MONITOR_ENABLED=False,
        OMS_WORKER_ENABLED=False, OMS_RECONCILIATION_ENABLED=False,
        DATA_LAKE_ROOT=str(tmp_path / "lake"), LOG_LEVEL="ERROR",
    )
    result = await validate_full_paper_flow(settings, direction, expected_side)
    assert result["side"] == expected_side
    assert result["pnl"] > 0


async def test_full_application_paper_safety_and_durable_halt(tmp_path):
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'safety.db'}",
        REDIS_URL=None, AGENT_WORKER_ENABLED=False, BACKFILL_WORKER_ENABLED=False,
        ENABLE_MARKET_DATA=False, OPERATIONS_MONITOR_ENABLED=False,
        OMS_WORKER_ENABLED=False, OMS_RECONCILIATION_ENABLED=False,
        ADMIN_API_KEY="isolated-paper-test-administration-key-32",
        DATA_LAKE_ROOT=str(tmp_path / "lake"), LOG_LEVEL="ERROR",
    )
    result = await validate_paper_safety(settings)
    assert result["kill_switch_durable"]

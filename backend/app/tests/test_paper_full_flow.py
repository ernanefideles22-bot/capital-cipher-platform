"""Full PAPER flow uses the same harness as the isolated PostgreSQL gate."""
from __future__ import annotations

import pytest
from app.core.config import Settings
from app.validation.paper_flow import validate_full_paper_flow, validate_paper_safety


@pytest.mark.parametrize("direction,expected_side", [(1, "BUY"), (-1, "SELL")])
@pytest.mark.parametrize("timeframe,strategy", [("15m", "SCALP_15M_v1"), ("1h", "DAY_1H_v1"), ("4h", "SWING_4H_v1")])
async def test_full_application_paper_flow_and_restart(tmp_path, direction, expected_side, timeframe, strategy):
    settings = Settings(
        DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'paper-flow.db'}",
        REDIS_URL=None, AGENT_WORKER_ENABLED=False, BACKFILL_WORKER_ENABLED=False,
        ENABLE_MARKET_DATA=False, OPERATIONS_MONITOR_ENABLED=False,
        OMS_WORKER_ENABLED=False, OMS_RECONCILIATION_ENABLED=False,
        DATA_LAKE_ROOT=str(tmp_path / "lake"), LOG_LEVEL="ERROR",
    )
    result = await validate_full_paper_flow(settings, direction, expected_side, timeframe=timeframe)
    assert result["side"] == expected_side
    assert result["timeframe"] == timeframe
    assert result["strategy"] == strategy
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


@pytest.mark.parametrize("timeframe", ["5m", "1d", "", "15m,1h"])
async def test_unsupported_fixture_timeframe_stops_before_application_start(monkeypatch, timeframe):
    from app.validation import paper_flow
    monkeypatch.setattr(paper_flow, "build_context", lambda *a, **k: pytest.fail("No application or database may start"))
    with pytest.raises(ValueError, match="PAPER_FIXTURE_TIMEFRAME_REFUSED"):
        await validate_full_paper_flow(None, 1, "BUY", timeframe=timeframe)

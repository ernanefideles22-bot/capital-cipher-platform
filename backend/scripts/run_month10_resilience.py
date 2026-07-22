"""Run the deterministic Month 10 PAPER load and chaos acceptance suite."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.api.context import build_context
from app.core.config import Settings
from app.operations.load_testing import AgentLoadHarness, DeterministicChaosHarness
from app.schemas.common import Exchange
from app.schemas.market import Candle


def _seed_market_data(context) -> None:
    end = datetime.now(timezone.utc)
    for index in range(240):
        close = 100.0 * (1.0005**index)
        context.candle_store.add(
            Candle(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                timeframe="15m",
                open=close * 0.999,
                high=close * 1.002,
                low=close * 0.998,
                close=close,
                volume=100.0 + index,
                closed_at=end - timedelta(minutes=15 * (239 - index)),
            )
        )


async def main() -> int:
    settings = Settings(
        AGENT_MAX_CONCURRENCY=32,
        AGENT_WORKER_ENABLED=False,
        BACKFILL_WORKER_ENABLED=False,
        OMS_WORKER_ENABLED=False,
        OMS_RECONCILIATION_ENABLED=False,
        OPERATIONS_MONITOR_ENABLED=False,
    )
    context = build_context(settings, with_database=False)
    assert context.agent_runtime is not None
    assert context.operations_service is not None
    _seed_market_data(context)
    await context.agent_runtime.initialize()

    runs = [
        await AgentLoadHarness(context.agent_runtime).run(
            environment="LOCAL",
        ),
        DeterministicChaosHarness.critical_database_outage(),
        DeterministicChaosHarness.optional_broker_outage(),
    ]
    for run in runs:
        await context.operations_service.record_resilience_run(run)

    payload = {
        "month": 10,
        "execution_mode": "PAPER",
        "live_execution_attempted": False,
        "registered_agents": len(context.agent_registry.registrations()),
        "runs": [run.model_dump(mode="json") for run in runs],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(run.status == "PASSED" for run in runs) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

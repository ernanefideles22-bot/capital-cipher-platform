"""Run the isolated seven-day-equivalent Month 11 PAPER campaign."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.api.context import build_context
from app.core.config import Settings
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.schemas.shadow_validation import ShadowCampaignDefinition
from app.shadow_validation.service import candle_dataset_fingerprint


def _candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(673):
        close = 100.0 * (1.0002**index) * (1 + 0.002 * ((index % 11) - 5))
        open_price = close * (0.999 if index % 2 else 1.001)
        rows.append(
            Candle(
                exchange=Exchange.BINANCE,
                symbol="BTCUSDT",
                timeframe="15m",
                open=open_price,
                high=max(open_price, close) * 1.002,
                low=min(open_price, close) * 0.998,
                close=close,
                volume=100 + (index % 37),
                closed_at=start + timedelta(minutes=15 * index),
            )
        )
    return rows


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
    candles = _candles()
    definition = ShadowCampaignDefinition(
        replay_start_at=candles[0].closed_at,
        replay_end_at=candles[-1].closed_at,
        replay_candle_count=len(candles),
        checkpoint_interval_candles=96,
        dataset_fingerprint=candle_dataset_fingerprint(candles),
    )
    report = await context.shadow_validation_service.run(definition, candles)
    payload = {
        "month": 11,
        "execution_mode": "PAPER",
        "live_execution_attempted": False,
        "registered_agents": len(context.agent_registry.registrations()),
        "report": report.model_dump(mode="json"),
        "checkpoints": [
            item.model_dump(mode="json")
            for item in reversed(
                context.shadow_validation_service.checkpoints(limit=100)
            )
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if report.status == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

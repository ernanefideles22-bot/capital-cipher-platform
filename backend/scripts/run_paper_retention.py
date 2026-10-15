"""Run the bounded PAPER retention workers once.

This process is intended for a Railway cron service with its own archive
volume. It never starts the API, agent runtime, OMS, TESTNET, or LIVE paths.
"""

from __future__ import annotations

import asyncio

from app.maintenance.paper_high_volume_retention import run_high_volume_from_env
from app.maintenance.paper_retention import run_from_env


async def _run() -> None:
    await run_from_env()
    await run_high_volume_from_env()


if __name__ == "__main__":
    asyncio.run(_run())

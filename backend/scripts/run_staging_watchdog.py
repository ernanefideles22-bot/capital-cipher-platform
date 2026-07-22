"""Continuously verify the externally visible staging PAPER invariants."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.operations.staging import evaluate_watchdog_snapshot


def _setting(name: str, *, default: str | None = None) -> str:
    value = os.environ.get(name, default or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    base_url = _setting("STAGING_BACKEND_URL").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("STAGING_BACKEND_URL must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("STAGING_BACKEND_URL must not contain credentials")
    admin_api_key = _setting("ADMIN_API_KEY")
    interval = float(_setting("STAGING_WATCHDOG_INTERVAL_SECONDS", default="30"))
    threshold = int(_setting("STAGING_WATCHDOG_FAILURE_THRESHOLD", default="3"))
    if not 5 <= interval <= 3600:
        raise RuntimeError("watchdog interval must be between 5 and 3600 seconds")
    if not 1 <= threshold <= 20:
        raise RuntimeError("watchdog failure threshold must be between 1 and 20")

    consecutive_failures = 0
    headers = {"X-API-Key": admin_api_key}
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            violations: list[str]
            try:
                ready_response, status_response, operations_response = await asyncio.gather(
                    client.get(f"{base_url}/ready"),
                    client.get(f"{base_url}/api/v1/status"),
                    client.get(f"{base_url}/api/v1/operations/status", headers=headers),
                )
                ready_response.raise_for_status()
                status_response.raise_for_status()
                operations_response.raise_for_status()
                violations = evaluate_watchdog_snapshot(
                    ready_response.json(),
                    status_response.json(),
                    operations_response.json(),
                )
            except Exception as exc:
                violations = [f"PROBE_{type(exc).__name__.upper()}"]

            if violations:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            print(
                json.dumps(
                    {
                        "event": "STAGING_PAPER_WATCHDOG",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "healthy": not violations,
                        "violations": violations,
                        "consecutive_failures": consecutive_failures,
                        "failure_threshold": threshold,
                        "live_execution_available": False,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if consecutive_failures >= threshold:
                raise RuntimeError("staging PAPER watchdog failure threshold reached")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())

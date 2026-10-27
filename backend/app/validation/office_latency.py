"""Paired PAPER benchmark: preserved synchronous path vs reserved fast lane.

All 300 real registry agents are evaluated per sample. Data is a deterministic
fixture, provider HTTP is mocked, and capital resets per sample. Trading latency
ends at the real risk/OMS/PAPER completion; deferred analytical completion is
measured separately. These are controlled local results, not hosted latency SLOs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from math import exp, sin
from pathlib import Path

import httpx

from app.agents.base import BaseAgent
from app.api.context import build_context
from app.core.config import Settings
from app.core.state_machine import SystemState
from app.office.isolation import IsolatedAgentRunner
from app.operations.metrics import percentile
from app.schemas.common import AgentStatus, Exchange, Signal, utcnow
from app.schemas.market import Candle
from app.validation.paper_flow import FIXTURE_INTERVALS, fixture_interval, trust_fixture_clock


class BenchmarkShadow(BaseAgent):
    name = "MomentumAgent"
    version = "2.0.0"
    capabilities = ("controlled-benchmark-delay",)
    max_attempts = 1

    async def _analyze(self, agent_input):
        await asyncio.sleep(.1)
        return self._output(AgentStatus.COMPLETED, Signal.HOLD, 50, "Controlled 100ms SHADOW fixture")


async def benchmark_sample(*, enabled, runner, primary_runner, timeframe="15m"):
    interval = fixture_interval(timeframe)
    config = Settings(_env_file=None, office_async_enabled=enabled,
        # The controlled replacement is a benchmark fixture, not a reviewed
        # production catalog implementation. Do not publish OfficeAnalysis for
        # it merely because the invoking shell supplies APP_SOURCE_REVISION.
        app_source_revision="",
        database_url="sqlite+aiosqlite:///:memory:", redis_url=None,
        enable_market_data=False, operations_monitor_enabled=False)
    context = build_context(config, with_database=False)
    context.agent_registry.replace(BenchmarkShadow(), expected_version="1.0.0")
    if enabled:
        context.agent_runtime.shadow_runner = runner
        context.agent_runtime.primary_runner = primary_runner
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503, request=req)))
    for agent in context.agent_registry.shadow_agents():
        collector = getattr(agent, "_derivatives_collector", None)
        if collector is not None:
            collector._client = client
    await context.state_machine.transition(SystemState.INITIALIZING, reason="benchmark", actor="isolated-fixture")
    await context.state_machine.transition(SystemState.PAPER, reason="benchmark", actor="isolated-fixture")
    initialize_started = time.perf_counter()
    await context.agent_runtime.initialize()
    initialization_ms = (time.perf_counter() - initialize_started) * 1000
    context.market_connected = True
    trust_fixture_clock(context)
    now = utcnow()
    candles = []
    for index in range(220):
        close = 100 * exp(.002 * index) * (1 + .01 * sin(1.1 * index))
        candle = Candle(exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe=timeframe,
            open=close * .999, high=close * 1.003, low=close * .997,
            close=close, volume=100, closed_at=now - interval * (219 - index))
        candles.append(candle)
        if index < 219:
            context.candle_store.add(candle)
    try:
        start = time.perf_counter()
        decision = await context.orchestrator.on_candle_closed(candles[-1])
        critical_ms = (time.perf_counter() - start) * 1000
        assert decision is not None
        assert decision.risk_status.value in {"APPROVED", "REDUCED"}
        assert decision.timeframe == timeframe
        assert decision.strategy == context.orchestrator.strategy_engine.select("BTCUSDT", timeframe).versioned_id
        assert len(context.paper_engine.open_orders) == 1
        order, = context.paper_engine.open_orders.values()
        assert order.approval_id and order.risk_check_id
        agents_at_decision = len(await context.agent_runtime.list_jobs(limit=1000))
        if enabled:
            await context.shadow_office.run_once()
            assert context.shadow_office.failed == 0
        completion_ms = (time.perf_counter() - start) * 1000
        jobs = await context.agent_runtime.list_jobs(limit=1000)
        assert len(jobs) == 300
        assert all(j.status == "COMPLETED" for j in jobs)
        assert not any(j.output.status in {AgentStatus.FAILED, AgentStatus.TIMEOUT} for j in jobs)
        return {"critical_ms": critical_ms, "analytical_completion_ms": completion_ms,
            "runtime_initialization_ms": initialization_ms,
            "jobs_at_order": agents_at_decision, "completed_jobs": len(jobs),
            "paper_orders": 1, "risk_and_oms_verified": True,
            "timeframe": timeframe, "strategy": decision.strategy}
    finally:
        await client.aclose()


async def compare(samples=40, *, timeframe="15m"):
    fixture_interval(timeframe)
    runner = IsolatedAgentRunner(concurrency=2, timeout_seconds=5)
    primary_runner = IsolatedAgentRunner(concurrency=1, timeout_seconds=3)
    results = {"before": [], "after": []}
    try:
        # Warm both paths; keep the same process pool for all after samples.
        cold_before = await benchmark_sample(enabled=False, runner=runner, primary_runner=primary_runner, timeframe=timeframe)
        cold_after = await benchmark_sample(enabled=True, runner=runner, primary_runner=primary_runner, timeframe=timeframe)
        for index in range(samples):
            # Alternate order to reduce monotonic warm-up/temperature bias.
            for enabled in ([False, True] if index % 2 == 0 else [True, False]):
                results["after" if enabled else "before"].append(await benchmark_sample(enabled=enabled, runner=runner, primary_runner=primary_runner, timeframe=timeframe))
    finally:
        await runner.aclose()
        await primary_runner.aclose()
    summary = {}
    for name, rows in results.items():
        values = [row["critical_ms"] for row in rows]
        summary[name] = {"samples": len(rows), **{key: round(percentile(values, p), 3)
            for key, p in (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99))},
            "max_ms": round(max(values), 3),
            "all_300_completed": all(row["completed_jobs"] == 300 for row in rows),
            "risk_oms_orders_verified": all(row["risk_and_oms_verified"] for row in rows)}
    revision = os.getenv("APP_SOURCE_REVISION", "")
    return {"schema_version": "office-latency-benchmark-v1", "summary": summary,
        "source_revision": revision if re.fullmatch(r"[a-f0-9]{40}", revision) else None,
        "fixture": {"agents": 300, "primary": 3, "shadow": 297, "timeframe": timeframe,
            "controlled_shadow_delay_ms": 100, "pool_processes": 2,
            "provider_http": "MockTransport 503", "durable_database": False,
            "analytical_context_cache": "not published for the controlled benchmark replacement",
            "hardware": "same local execution host; paired alternating trials"},
        "limitations": ["Not hosted staging measurements; no post-deploy claim",
            "Critical timer starts after initialization; reserved process warmup is reported separately",
            "Local percentiles cannot be subtracted from the hosted 92s mean",
            "Finite small sample; P99 is exploratory, not a latency SLO",
            "Synthetic PAPER fills do not demonstrate financial performance"],
        "cold_start_samples": {"before": cold_before, "after": cold_after},
        "raw_samples": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--timeframe", choices=tuple(FIXTURE_INTERVALS), default="15m")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if not 10 <= args.samples <= 1000:
        parser.error("samples must be 10..1000")
    report = asyncio.run(compare(args.samples, timeframe=args.timeframe))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()

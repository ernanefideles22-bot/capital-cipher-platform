"""Measure PRIMARY persistence locally; never accesses a hosted database.

Three deterministic PRIMARY agents, temporary SQLite, fake Redis transport,
and optional asynchronous latency per SQL statement/commit call. This measures
the durable agent cohort, not a trading decision, network RTT or profitability.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
import time
from pathlib import Path

from sqlalchemy import event

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.core.event_bus import EventBus
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.office.isolation import IsolatedAgentRunner
from app.operations.metrics import percentile
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import AgentStatus, Signal


class ControlledPrimary(BaseAgent):
    decision_role = "PRIMARY"

    async def _analyze(self, agent_input):
        return self._output(AgentStatus.COMPLETED, Signal.HOLD, 50, "Persistence fixture")


class FixtureTransport:
    async def publish_many(self, messages):
        return ["fixture-" + message.event_id for message in messages]


async def sample(delay):
    with tempfile.TemporaryDirectory(prefix="cc-primary-local-") as directory:
        database = Database("sqlite+aiosqlite:///" + str(Path(directory) / "probe.db"))
        await database.create_all()
        repository = Repository(database)
        bus = EventBus(journal_many=repository.save_bus_messages, transport=FixtureTransport(),
            transport_required=True, mark_published_many=repository.mark_bus_messages_published,
            mark_failed_many=repository.mark_bus_messages_failed)
        agents = [ControlledPrimary() for _ in range(3)]
        for index, agent in enumerate(agents):
            agent.name = "ControlledPrimary" + str(index)
        runtime = AgentRuntime(AgentRegistry(agents), repository=repository, event_bus=bus)
        runtime.primary_runner = IsolatedAgentRunner(concurrency=1, timeout_seconds=3)
        runtime.shadow_runner = IsolatedAgentRunner(concurrency=2)
        try:
            started = time.monotonic()
            await runtime.initialize()
            initialize_ms = (time.monotonic() - started) * 1000
            if delay:
                def latency(connection, *args):
                    connection.connection.dbapi_connection.run_async(lambda _: asyncio.sleep(delay))
                event.listen(database.engine.sync_engine, "before_cursor_execute", latency)
                event.listen(database.engine.sync_engine, "commit", latency)
            requests = [AgentExecutionRequest(agent_version=agent.version,
                input=AgentInput(agent_name=agent.name, request_id="persistence-" + agent.name,
                    correlation_id="persistence-fixture", symbol="BTCUSDT", timeframe="15m")) for agent in agents]
            outputs = await runtime.execute_primary(requests)
            profile = runtime.last_primary_profile
            assert len(outputs) == 3 and all(output.status == AgentStatus.COMPLETED for output in outputs)
            for job in await runtime.list_jobs():
                trace = await runtime.trace(job.execution_id)
                assert len(trace.attempts) == 1 and len(trace.memory) == 3
            return {"initialize_ms": round(initialize_ms, 3), "profile": profile,
                    "injected_sql_and_commit_ms": delay * 1000, "exact_once_verified": True}
        finally:
            await runtime.primary_runner.aclose()
            await runtime.shadow_runner.aclose()
            await database.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--delay-ms", type=float, default=140)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100 or not 0 <= args.delay_ms <= 500:
        parser.error("samples must be 5..100 and delay-ms 0..500")
    logging.disable(logging.CRITICAL)
    async def run():
        return [await sample(args.delay_ms / 1000) for _ in range(args.samples)]
    rows = asyncio.run(run())
    times = [row["profile"]["elapsed_ms"] for row in rows]
    summary = {name: round(percentile(times, p), 3) for name, p in
               (("p50_ms", .5), ("p95_ms", .95), ("p99_ms", .99))}
    report = {"fixture": __doc__, "summary": summary, "samples": rows,
              "limitations": ["Local synthetic latency, not Railway or PostgreSQL RTT",
                  "Cohort persistence only; does not execute risk/OMS or PAPER orders",
                  "Does not certify the three-second deadline; startup reported separately",
                  "SQL counters omit driver handshake, BEGIN, pre-ping and network traffic",
                  "Percentiles on small samples are exploratory"]}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()

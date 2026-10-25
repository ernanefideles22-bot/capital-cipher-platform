"""Bounded observational admission queue over the existing durable AgentRuntime.

Admission is best-effort, bounded and explicitly counted. Once admitted, jobs,
attempts and outputs use the unchanged durable repository and recoverable leases.
No order, risk approval or operational strategy is created by this scheduler.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque, OrderedDict
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from app.schemas.agents import AgentExecutionRequest, AgentInput, agent_execution_fingerprint
from app.schemas.common import utcnow, AgentStatus
from app.office.contracts import OfficeAnalysis
from app.office.catalog import _affiliation
from app.core.logging import ServiceLogger

logger = ServiceLogger("office-analytics")


def shadow_correlation(candle):
    key = (candle.exchange.value, candle.symbol, candle.timeframe, candle.closed_at.isoformat())
    return str(uuid5(NAMESPACE_URL, "office-shadow:" + ":".join(key)))


class ShadowOffice:
    def __init__(self, runtime, *, evaluation=None, operations=None, capacity=12, ttl_seconds=120, source_revision=None):
        self.runtime = runtime
        self.evaluation = evaluation
        self.operations = operations
        self._queue = asyncio.PriorityQueue(maxsize=capacity)
        self._sequence = itertools.count()
        self._pending = set()
        self._completed = deque(maxlen=1000)
        self.ttl_seconds = ttl_seconds
        self.task = None
        self.accepted = self.rejected = self.expired = self.failed = self.completed = 0
        self.source_revision = source_revision
        self.contexts = OrderedDict()

    def offer(self, candle, primary_outputs=()):
        key = (candle.exchange.value, candle.symbol, candle.timeframe, candle.closed_at.isoformat())
        if key in self._pending or key in self._completed:
            return False
        if self._queue.full():
            self.rejected += 1
            return False
        priority = {"15m": 0, "1h": 1, "4h": 2}.get(candle.timeframe, 3)
        self._queue.put_nowait((priority, next(self._sequence), key, candle, list(primary_outputs)))
        self._pending.add(key)
        self.accepted += 1
        return True

    async def run_once(self):
        _, _, key, candle, primary = await self._queue.get()
        started = time.monotonic()
        try:
            deadline = candle.closed_at + timedelta(seconds=self.ttl_seconds)
            if utcnow() >= deadline:
                self.expired += 1
                return
            correlation = shadow_correlation(candle)
            registrations = self.runtime.registry.shadow_agents()
            requests = [AgentExecutionRequest(
                agent_version=agent.version,
                idempotency_key=f"{correlation}:{agent.name}",
                input=AgentInput(agent_name=agent.name, correlation_id=correlation,
                    request_id=f"{correlation}:{agent.name}", timestamp=candle.closed_at,
                    symbol=candle.symbol, timeframe=candle.timeframe,
                    market_context={"exchange": candle.exchange.value},
                    config={"office_deadline_at": deadline.isoformat()}),
            ) for agent in registrations if agent.enabled]
            outputs = await self.runtime.execute_many(requests)
            if self.source_revision and utcnow() < deadline:
                by_name = {r.input.agent_name: r for r in requests}
                for output in outputs:
                    if output.status != AgentStatus.COMPLETED:
                        continue
                    agent = self.runtime.registry.get(output.agent_name)
                    identity = agent_execution_fingerprint(by_name[agent.name])
                    analysis = OfficeAnalysis(analysis_id=identity, correlation_id=correlation,
                        agent_name=agent.name, agent_version=agent.version,
                        source_revision=self.source_revision, definition_hash=agent.registration().definition_hash,
                        department=_affiliation(agent)[0], symbol=candle.symbol, timeframe=candle.timeframe,
                        direction=output.signal.value if output.signal.value in {"BUY", "SELL", "HOLD", "WAIT", "NEUTRAL"} else "WAIT",
                        confirmation=(output.reason,), invalidation=("Expiry, new market evidence or definition change",),
                        evidence_ids=(identity,), as_of=candle.closed_at, published_at=utcnow(),
                        expires_at=deadline, max_age_seconds=self.ttl_seconds)
                    cache_key = (agent.name, candle.symbol, candle.timeframe)
                    self.contexts[cache_key] = analysis
                    self.contexts.move_to_end(cache_key)
                    while len(self.contexts) > 3000:
                        self.contexts.popitem(last=False)
            if self.evaluation is not None:
                await self.evaluation.observe(candle=candle, correlation_id=correlation,
                    outputs=[*primary, *outputs], registrations={
                        r.agent_name: r for r in self.runtime.registry.registrations()})
            if self.operations is not None:
                await self.operations.observe_agent_batch([*primary, *outputs], correlation_id=correlation,
                    duration_ms=(time.monotonic() - started) * 1000)
            self.completed += 1
            self._completed.append(key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed += 1
            logger.error("Analytical batch unavailable; fast lane is independent",
                event_type="OFFICE_ANALYTICS_FAILED", correlation_id=shadow_correlation(candle),
                metadata={"error_type": type(exc).__name__, "symbol": candle.symbol,
                          "timeframe": candle.timeframe})
        finally:
            self._pending.discard(key)
            self._queue.task_done()

    async def _run(self):
        while True:
            await self.run_once()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run(), name="office-shadow-admission")

    async def recover(self, repository, subscriptions):
        """Rebuild interrupted admission from durable candles, never replay trading.

        Deterministic analytical identities deduplicate previously admitted jobs.
        Expired candles are deliberately excluded; their histories remain intact.
        """
        now = utcnow()
        for symbol, timeframe in subscriptions:
            candles = await repository.list_candles(exchange="BINANCE", symbol=symbol,
                timeframe=timeframe, start_at=now - timedelta(seconds=self.ttl_seconds),
                end_at=now, limit=100)
            for candle in candles:
                self.offer(candle)

    async def aclose(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def status(self):
        return {"queued": self._queue.qsize(), "accepted": self.accepted,
                "rejected": self.rejected, "expired": self.expired,
                "failed": self.failed, "completed": self.completed,
                "valid_contexts": sum(item.usable_at(utcnow()) for item in self.contexts.values())}

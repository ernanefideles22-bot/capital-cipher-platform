"""Supervised agent analysis in bounded, disposable Python processes.

Only candle/evidence snapshots cross this boundary: no Repository, OMS, risk
manager, credentials or network collector. A stuck CPU loop is killed, not merely
cancelled in the API event loop. This is fault isolation, not a hostile-code jail.
"""
from __future__ import annotations

import asyncio
import copy
import multiprocessing as mp
import time
from dataclasses import dataclass

from app.schemas.agents import AgentOutput
from app.schemas.common import AgentStatus, Signal, utcnow


@dataclass
class CandleSnapshot:
    candles: list

    def get(self, exchange, symbol, timeframe, limit=100):
        return [c for c in self.candles if c.exchange.value == exchange
                and c.symbol == symbol and c.timeframe == timeframe][-limit:]


@dataclass
class EvidenceSnapshot:
    evidence: object

    async def latest(self, **kwargs):
        return self.evidence


@dataclass
class ConnectionSnapshot:
    status: str

    def __call__(self):
        return self.status


def _process_loop(pipe):
    while True:
        try:
            agent, agent_input = pipe.recv()
        except EOFError:
            return
        if agent is None and agent_input is None:
            pipe.send("office-ready-v1")
            continue
        try:
            output = asyncio.run(agent.run(agent_input))
            pipe.send(output.model_dump_json())
        except BaseException:
            # No exception text or arbitrary object is exported from the child.
            pipe.send(None)


class IsolatedAgentRunner:
    def __init__(self, *, concurrency=2, timeout_seconds=5.0, breaker_failures=3, cooldown_seconds=60):
        self._slots = asyncio.Queue()
        for _ in range(concurrency):
            self._slots.put_nowait(None)
        self._context = mp.get_context("spawn")
        self._timeout = timeout_seconds
        self._threshold = breaker_failures
        self._cooldown = cooldown_seconds
        self._breakers = {}
        self._children = set()
        self.timeouts = 0
        self.closed = False
        self.last_warmup_ms = None

    async def _receive(self, slot):
        while not slot[1].poll():
            if not slot[0].is_alive():
                raise RuntimeError("Agent process exited")
            await asyncio.sleep(.002)
        return await asyncio.to_thread(slot[1].recv)

    async def warmup(self, *, timeout_seconds=5):
        """Await one ready reserved process before trading is admitted.

        Same process/slot budget as lazy startup. No agent or market analysis is
        run. Failure/cancellation disposes the child and prevents successful boot.
        """
        if self.closed:
            raise RuntimeError("Agent executor closed")
        slot = await self._slots.get()
        start = time.monotonic()
        try:
            async def ready():
                nonlocal slot
                if slot is None or not slot[0].is_alive():
                    if slot is not None:
                        await self._dispose(slot)
                    slot = self._start()
                await asyncio.to_thread(slot[1].send, (None, None))
                if await self._receive(slot) != "office-ready-v1":
                    raise RuntimeError("Agent warmup handshake failed")
            await asyncio.wait_for(ready(), timeout=timeout_seconds)
            self.last_warmup_ms = round((time.monotonic() - start) * 1000, 3)
        except BaseException:
            if slot is not None:
                await self._dispose(slot)
                slot = None
            raise
        finally:
            if self.closed and slot is not None:
                await self._dispose(slot)
                slot = None
            self._slots.put_nowait(slot)

    def _start(self):
        parent, child = self._context.Pipe()
        process = self._context.Process(target=_process_loop, args=(child,), daemon=True)
        process.start()
        child.close()
        self._children.add(process)
        return process, parent

    async def _dispose(self, slot):
        process, pipe = slot
        pipe.close()
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, .25)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, .25)
        self._children.discard(process)
        if not process.is_alive():
            process.close()

    async def _snapshot(self, agent, agent_input):
        clone = copy.copy(agent)
        clone.__dict__ = dict(agent.__dict__)
        clone.last_input = clone.last_output = None
        if hasattr(agent, "_connection_status_fn"):
            clone._connection_status_fn = ConnectionSnapshot(agent._connection_status_fn())
        if hasattr(agent, "_store"):
            candles = agent._store.get(agent_input.market_context.get("exchange", "BINANCE"),
                                       agent_input.symbol, agent_input.timeframe, limit=500)
            clone._store = CandleSnapshot([c for c in candles if c.closed_at <= agent_input.timestamp])
        if hasattr(agent, "_evidence_service"):
            await asyncio.wait_for(agent._prefetch_governed_evidence(agent_input),
                                   timeout=agent.evidence_prefetch_timeout_ms / 1000)
            definition = agent._definition
            evidence = await agent._evidence_service.latest(
                domain=definition.domain, metric_name=definition.metric_name,
                scope="GLOBAL" if definition.scope == "GLOBAL" else agent_input.symbol,
                as_of=agent_input.timestamp,
            )
            clone._evidence_service = EvidenceSnapshot(evidence)
            clone._derivatives_collector = None
        return clone

    async def run(self, agent, agent_input):
        failures, until = self._breakers.get(agent.name, (0, 0))
        if until > time.monotonic():
            return AgentOutput(agent_name=agent.name, status=AgentStatus.SKIPPED,
                               signal=Signal.WAIT, confidence=0, reason="Agent circuit breaker open",
                               warnings=["OFFICE_CIRCUIT_OPEN"])
        if self.closed:
            raise RuntimeError("Agent executor closed")
        slot = await self._slots.get()
        started = time.monotonic()
        agent.status = "RUNNING"
        agent.total_runs += 1
        agent.last_run_at = utcnow()
        agent.last_input = agent_input
        broken = False
        try:
            if slot is None or not slot[0].is_alive():
                if slot is not None:
                    await self._dispose(slot)
                slot = self._start()
            async def execute():
                clone = await self._snapshot(agent, agent_input)
                await asyncio.to_thread(slot[1].send, (clone, agent_input))
                raw = await self._receive(slot)
                if raw is None:
                    raise RuntimeError("Agent process failed")
                result = AgentOutput.model_validate_json(raw)
                if result.agent_name != agent.name:
                    raise ValueError("Agent output identity mismatch")
                return result
            output = await asyncio.wait_for(execute(), timeout=self._timeout)
        except asyncio.CancelledError:
            broken = True
            raise
        except Exception as exc:
            broken = True
            timeout = isinstance(exc, TimeoutError)
            self.timeouts += int(timeout)
            output = AgentOutput(agent_name=agent.name,
                status=AgentStatus.TIMEOUT if timeout else AgentStatus.FAILED,
                signal=Signal.WAIT, confidence=0, reason="Isolated agent execution unavailable",
                warnings=["OFFICE_PROCESS_TIMEOUT" if timeout else "OFFICE_PROCESS_FAILED"])
        finally:
            if broken and slot is not None:
                await self._dispose(slot)
                slot = None
            self._slots.put_nowait(slot)
        elapsed = (time.monotonic() - started) * 1000
        output = output.model_copy(update={"latency_ms": round(elapsed)})
        failed = output.status in {AgentStatus.TIMEOUT, AgentStatus.FAILED}
        agent.total_latency_ms += elapsed
        agent.total_failures += int(failed)
        agent.status = output.status.value if failed else "READY"
        agent.last_output = output
        agent.last_signal, agent.last_confidence = output.signal.value, output.confidence
        if failed:
            agent.last_failure_at = utcnow()
            failures += 1
            self._breakers[agent.name] = (failures, time.monotonic() + self._cooldown if failures >= self._threshold else 0)
        else:
            self._breakers.pop(agent.name, None)
        return output

    async def aclose(self):
        self.closed = True
        while not self._slots.empty():
            slot = self._slots.get_nowait()
            if slot is not None:
                await self._dispose(slot)

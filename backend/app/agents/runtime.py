"""Isolated, idempotent and traceable PAPER agent runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import uuid
from datetime import timedelta
from typing import Protocol

from app.agents.base import BaseAgent
from app.agents.registry import AgentRegistry
from app.core.errors import DatabaseError, ValidationError
from app.core.event_bus import EventBus, EventPublication, Topics
from app.core.logging import ServiceLogger
from app.core.payload_security import ensure_payload_has_no_secrets
from app.schemas.agents import (
    AgentExecutionAttempt,
    AgentExecutionFinish,
    AgentExecutionJob,
    AgentExecutionRequest,
    AgentExecutionTrace,
    AgentMemoryEntry,
    AgentOutput,
    agent_execution_fingerprint,
)
from app.schemas.common import AgentStatus, Signal, utcnow
from app.schemas.events import EventTypes

logger = ServiceLogger("agent-runtime")
_LIFECYCLE_EVENT_NAMESPACE = uuid.UUID("fe2048d3-e905-5dd7-9a0b-57bf7a3b08c8")


def agent_lifecycle_event_id(
    execution_id: str,
    event_type: str,
    attempt_number: int,
) -> str:
    """Return a deterministic UUID that fits the durable event journal."""

    identity = (
        f"capital-cipher:agent-runtime:{execution_id}:"
        f"{event_type}:{attempt_number}"
    )
    return str(uuid.uuid5(_LIFECYCLE_EVENT_NAMESPACE, identity))


class AgentRuntimeRepository(Protocol):
    async def create_agent_execution(
        self,
        job: AgentExecutionJob,
        input_memory: AgentMemoryEntry,
    ) -> AgentExecutionJob: ...

    async def create_agent_executions(
        self,
        submissions: list[tuple[AgentExecutionJob, AgentMemoryEntry]],
    ) -> list[AgentExecutionJob]: ...

    async def claim_agent_execution(
        self,
        execution_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None: ...

    async def claim_next_agent_execution(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None: ...

    async def claim_next_agent_executions(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int,
    ) -> list[AgentExecutionJob]: ...

    async def finish_agent_execution(
        self,
        *,
        attempt: AgentExecutionAttempt,
        attempt_memory: AgentMemoryEntry,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
        retry_delay_seconds: float,
        terminal_memory: AgentMemoryEntry | None,
    ) -> AgentExecutionJob: ...

    async def finish_agent_executions(
        self,
        finishes: list[AgentExecutionFinish],
    ) -> list[AgentExecutionJob]: ...

    async def load_agent_execution_trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None: ...

    async def load_agent_execution_traces(
        self,
        execution_ids: list[str],
    ) -> dict[str, AgentExecutionTrace]: ...

    async def list_agent_execution_jobs(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentExecutionJob]: ...


def _payload_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _memory(
    *,
    execution_id: str,
    sequence: int,
    entry_type: str,
    payload: dict,
) -> AgentMemoryEntry:
    return AgentMemoryEntry(
        execution_id=execution_id,
        sequence=sequence,
        entry_type=entry_type,
        payload=payload,
        payload_hash=_payload_hash(payload),
    )


class InMemoryAgentRuntimeRepository:
    """Contract-equivalent fallback used when no database is configured."""

    def __init__(self) -> None:
        self._jobs: dict[str, AgentExecutionJob] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._attempts: dict[str, list[AgentExecutionAttempt]] = {}
        self._memory: dict[str, list[AgentMemoryEntry]] = {}
        self._lock = asyncio.Lock()

    async def create_agent_execution(
        self,
        job: AgentExecutionJob,
        input_memory: AgentMemoryEntry,
    ) -> AgentExecutionJob:
        async with self._lock:
            return self._create_agent_execution(job, input_memory)

    def _create_agent_execution(
        self,
        job: AgentExecutionJob,
        input_memory: AgentMemoryEntry,
    ) -> AgentExecutionJob:
        key = (job.agent_name, job.agent_version, job.idempotency_key)
        existing_id = self._idempotency.get(key)
        if existing_id is not None:
            existing = self._jobs[existing_id]
            if existing.request_fingerprint != job.request_fingerprint:
                raise ValidationError(
                    "Agent execution idempotency key conflicts with "
                    "different input"
                )
            return existing
        self._jobs[job.execution_id] = job
        self._idempotency[key] = job.execution_id
        self._attempts[job.execution_id] = []
        self._memory[job.execution_id] = [input_memory]
        return job

    async def create_agent_executions(
        self,
        submissions: list[tuple[AgentExecutionJob, AgentMemoryEntry]],
    ) -> list[AgentExecutionJob]:
        async with self._lock:
            return [
                self._create_agent_execution(job, input_memory)
                for job, input_memory in submissions
            ]

    def _claim(
        self,
        execution_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None:
        now = utcnow()
        job = self._jobs.get(execution_id)
        if job is None:
            return None
        ready = (
            job.status in {"PENDING", "RETRY"}
            and job.available_at <= now
        )
        expired = (
            job.status == "LEASED"
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        )
        if not (ready or expired):
            return None
        if ready and job.attempt_count >= job.max_attempts:
            return None
        next_attempt = (
            job.attempt_count
            if expired
            else job.attempt_count + 1
        )
        claimed = job.model_copy(
            update={
                "status": "LEASED",
                "attempt_count": next_attempt,
                "leased_by": worker_id,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        self._jobs[execution_id] = claimed
        return claimed

    async def claim_agent_execution(
        self,
        execution_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None:
        async with self._lock:
            return self._claim(
                execution_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    async def claim_next_agent_execution(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None:
        claimed = await self.claim_next_agent_executions(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            limit=1,
        )
        return claimed[0] if claimed else None

    async def claim_next_agent_executions(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int,
    ) -> list[AgentExecutionJob]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Agent claim limit must be 1..1000")
        async with self._lock:
            ordered = sorted(
                self._jobs.values(),
                key=lambda item: (item.available_at, item.created_at),
            )
            claimed_jobs: list[AgentExecutionJob] = []
            for job in ordered:
                claimed = self._claim(
                    job.execution_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                if claimed is not None:
                    claimed_jobs.append(claimed)
                    if len(claimed_jobs) == limit:
                        break
            return claimed_jobs

    async def finish_agent_execution(
        self,
        *,
        attempt: AgentExecutionAttempt,
        attempt_memory: AgentMemoryEntry,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
        retry_delay_seconds: float,
        terminal_memory: AgentMemoryEntry | None,
    ) -> AgentExecutionJob:
        finishes = [
            AgentExecutionFinish(
                attempt=attempt,
                attempt_memory=attempt_memory,
                worker_id=worker_id,
                output=output,
                retryable=retryable,
                retry_delay_seconds=retry_delay_seconds,
                terminal_memory=terminal_memory,
            )
        ]
        updated = await self.finish_agent_executions(finishes)
        return updated[0]

    def _validate_finish(
        self,
        finish: AgentExecutionFinish,
    ) -> None:
        execution_id = finish.attempt.execution_id
        job = self._jobs[execution_id]
        if job.status != "LEASED" or job.leased_by != finish.worker_id:
            raise ValidationError("Agent execution lease ownership lost")
        attempts = self._attempts[execution_id]
        expected = len(attempts) + 1
        if (
            finish.attempt.attempt_number != expected
            or job.attempt_count != finish.attempt.attempt_number
        ):
            raise ValidationError("Agent attempts must be append-only")

    def _finish(
        self,
        finish: AgentExecutionFinish,
        *,
        now,
    ) -> AgentExecutionJob:
        execution_id = finish.attempt.execution_id
        job = self._jobs[execution_id]
        attempts = self._attempts[execution_id]
        attempts.append(finish.attempt)
        self._memory[execution_id].append(finish.attempt_memory)
        successful = finish.output.status not in {
            AgentStatus.FAILED,
            AgentStatus.TIMEOUT,
        }
        will_retry = (
            not successful
            and finish.retryable
            and job.attempt_count < job.max_attempts
        )
        status = (
            "COMPLETED"
            if successful
            else "RETRY"
            if will_retry
            else "DEAD_LETTER"
        )
        error_code = (
            None
            if successful
            else "AGENT_TIMEOUT"
            if finish.output.status == AgentStatus.TIMEOUT
            else "AGENT_FAILED"
        )
        updated = job.model_copy(
            update={
                "status": status,
                "available_at": (
                    now
                    + timedelta(seconds=finish.retry_delay_seconds)
                    if will_retry
                    else now
                ),
                "leased_by": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "output": finish.output,
                "updated_at": now,
                "completed_at": (
                    now if status in {"COMPLETED", "DEAD_LETTER"} else None
                ),
            }
        )
        self._jobs[execution_id] = updated
        if finish.terminal_memory is not None:
            self._memory[execution_id].append(finish.terminal_memory)
        return updated

    async def finish_agent_executions(
        self,
        finishes: list[AgentExecutionFinish],
    ) -> list[AgentExecutionJob]:
        if not finishes:
            return []
        execution_ids = [
            finish.attempt.execution_id for finish in finishes
        ]
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("Agent finish execution_ids must be unique")
        now = utcnow()
        async with self._lock:
            for finish in finishes:
                self._validate_finish(finish)
            return [
                self._finish(finish, now=now)
                for finish in finishes
            ]

    async def load_agent_execution_trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None:
        traces = await self.load_agent_execution_traces([execution_id])
        return traces.get(execution_id)

    async def load_agent_execution_traces(
        self,
        execution_ids: list[str],
    ) -> dict[str, AgentExecutionTrace]:
        if not execution_ids:
            return {}
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("Agent trace execution_ids must be unique")
        async with self._lock:
            return {
                execution_id: AgentExecutionTrace(
                    job=self._jobs[execution_id],
                    attempts=list(self._attempts[execution_id]),
                    memory=list(self._memory[execution_id]),
                )
                for execution_id in execution_ids
                if execution_id in self._jobs
            }

    async def list_agent_execution_jobs(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentExecutionJob]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Agent execution limit must be 1..1000")
        async with self._lock:
            return sorted(
                self._jobs.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )[:limit]


class AgentRuntime:
    """Run validated agents with bounded retries and per-execution memory."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        repository: AgentRuntimeRepository | None = None,
        event_bus: EventBus | None = None,
        lease_seconds: int = 30,
        retry_base_seconds: float = 0.05,
        retry_max_seconds: float = 0.2,
        max_concurrency: int = 8,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("Agent lease_seconds must be positive")
        if retry_base_seconds < 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Agent retry delay settings are inconsistent")
        if max_concurrency < 1:
            raise ValueError("Agent max_concurrency must be positive")
        self.registry = registry
        self.repository = repository or InMemoryAgentRuntimeRepository()
        self._event_bus = event_bus
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._submission_semaphore = asyncio.Semaphore(max_concurrency)
        self._agent_locks: dict[str, asyncio.Lock] = {
            name: asyncio.Lock() for name in registry.agents
        }
        self._active_background_workers = 0
        self._completion_waiters: dict[str, set[asyncio.Event]] = {}

    async def initialize(self) -> None:
        await self.registry.initialize_all()

    @property
    def background_worker_active(self) -> bool:
        return self._active_background_workers > 0

    def _background_worker_started(self) -> None:
        self._active_background_workers += 1

    def _background_worker_stopped(self) -> None:
        self._active_background_workers = max(
            0,
            self._active_background_workers - 1,
        )
        if self._active_background_workers == 0:
            for waiters in self._completion_waiters.values():
                for waiter in waiters:
                    waiter.set()

    def _register_completion_waiter(
        self,
        execution_id: str,
    ) -> asyncio.Event:
        waiter = asyncio.Event()
        self._completion_waiters.setdefault(execution_id, set()).add(waiter)
        return waiter

    def _remove_completion_waiter(
        self,
        execution_id: str,
        waiter: asyncio.Event,
    ) -> None:
        waiters = self._completion_waiters.get(execution_id)
        if waiters is None:
            return
        waiters.discard(waiter)
        if not waiters:
            self._completion_waiters.pop(execution_id, None)

    def _notify_completion(self, execution_id: str) -> None:
        for waiter in self._completion_waiters.get(execution_id, set()):
            waiter.set()

    @staticmethod
    def _worker_id(prefix: str) -> str:
        identity = (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        safe = re.sub(r"[^A-Za-z0-9._:-]", "-", identity)
        return f"{prefix}-{safe}"[:128]

    async def submit(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionJob:
        job, input_memory = self._prepare_submission(request)
        stored = await self.repository.create_agent_execution(
            job,
            input_memory,
        )
        await self._publish_requested(stored)
        return stored

    def _prepare_submission(
        self,
        request: AgentExecutionRequest,
    ) -> tuple[AgentExecutionJob, AgentMemoryEntry]:
        ensure_payload_has_no_secrets(
            request.model_dump(mode="json")
        )
        agent = self.registry.get(
            request.input.agent_name,
            version=request.agent_version,
        )
        registration = agent.registration()
        if request.execution_mode != registration.execution_mode:
            raise ValidationError("Agent execution mode does not match registry")
        fingerprint = agent_execution_fingerprint(request)
        assert request.idempotency_key is not None
        now = utcnow()
        job = AgentExecutionJob(
            execution_id=fingerprint,
            request_fingerprint=fingerprint,
            idempotency_key=request.idempotency_key,
            correlation_id=request.input.correlation_id,
            agent_name=registration.agent_name,
            agent_version=registration.version,
            agent_definition_hash=registration.definition_hash,
            decision_role=registration.decision_role,
            critical=registration.critical,
            input=request.input,
            max_attempts=registration.max_attempts,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        return (
            job,
            _memory(
                execution_id=fingerprint,
                sequence=1,
                entry_type="INPUT",
                payload=request.model_dump(mode="json"),
            ),
        )

    async def _publish_requested(self, job: AgentExecutionJob) -> None:
        await self._publish(
            EventTypes.AGENT_REQUESTED,
            job,
            event_id=agent_lifecycle_event_id(
                job.execution_id,
                EventTypes.AGENT_REQUESTED,
                0,
            ),
        )

    async def submit_many(
        self,
        requests: list[AgentExecutionRequest],
    ) -> list[AgentExecutionJob]:
        submissions = [
            self._prepare_submission(request) for request in requests
        ]
        jobs = await self.repository.create_agent_executions(submissions)
        unique_jobs = list(
            {
                job.execution_id: job
                for job in jobs
            }.values()
        )
        await self._publish_many(
            [
                self._event_publication(
                    EventTypes.AGENT_REQUESTED,
                    job,
                    event_id=agent_lifecycle_event_id(
                        job.execution_id,
                        EventTypes.AGENT_REQUESTED,
                        0,
                    ),
                )
                for job in unique_jobs
            ]
        )
        return jobs

    async def enqueue(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionJob:
        return await self.submit(request)

    async def _submit_bounded(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionJob:
        async with self._submission_semaphore:
            return await self.submit(request)

    async def execute(
        self,
        request: AgentExecutionRequest,
    ) -> AgentExecutionTrace:
        job = await self._submit_bounded(request)
        return await self._execute_submitted(job)

    async def _execute_submitted(
        self,
        job: AgentExecutionJob,
    ) -> AgentExecutionTrace:
        worker_id = self._worker_id("inline-agent")
        completion_waiter = self._register_completion_waiter(
            job.execution_id
        )
        try:
            while True:
                trace = await self.repository.load_agent_execution_trace(
                    job.execution_id
                )
                if trace is None:
                    raise DatabaseError("Agent execution disappeared")
                if trace.job.status in {"COMPLETED", "DEAD_LETTER"}:
                    return trace
                if self.background_worker_active:
                    # Local workers notify exact waiters on terminal writes.
                    # A lease-duration timeout covers distributed completion
                    # without recreating the database polling thundering herd.
                    try:
                        await asyncio.wait_for(
                            completion_waiter.wait(),
                            timeout=float(self._lease_seconds),
                        )
                    except TimeoutError:
                        pass
                    continue
                now = utcnow()
                if (
                    trace.job.status == "LEASED"
                    and (
                        trace.job.lease_expires_at is None
                        or trace.job.lease_expires_at > now
                    )
                ):
                    await asyncio.sleep(0.01)
                    continue
                if trace.job.available_at > now:
                    await asyncio.sleep(
                        min(
                            0.2,
                            (
                                trace.job.available_at - now
                            ).total_seconds(),
                        )
                    )
                    continue
                claimed = await self.repository.claim_agent_execution(
                    job.execution_id,
                    worker_id=worker_id,
                    lease_seconds=self._lease_seconds,
                )
                if claimed is None:
                    await asyncio.sleep(0)
                    continue
                await self.execute_claimed(claimed, worker_id=worker_id)
        finally:
            self._remove_completion_waiter(
                job.execution_id,
                completion_waiter,
            )

    async def execute_many(
        self,
        requests: list[AgentExecutionRequest],
    ) -> list[AgentOutput]:
        jobs = await self.submit_many(requests)
        if self.background_worker_active:
            traces = await self._wait_for_submitted_many(jobs)
        else:
            traces = await asyncio.gather(
                *(self._execute_submitted(job) for job in jobs)
            )
        outputs: list[AgentOutput] = []
        for trace in traces:
            if trace.job.output is None:
                raise DatabaseError("Terminal agent execution has no output")
            outputs.append(trace.job.output)
        return outputs

    async def _wait_for_submitted_many(
        self,
        jobs: list[AgentExecutionJob],
    ) -> list[AgentExecutionTrace]:
        execution_ids = [job.execution_id for job in jobs]
        unique_execution_ids = list(dict.fromkeys(execution_ids))
        waiters = {
            execution_id: self._register_completion_waiter(execution_id)
            for execution_id in unique_execution_ids
        }
        terminal: dict[str, AgentExecutionTrace] = {}
        try:
            while len(terminal) < len(unique_execution_ids):
                pending_ids = [
                    execution_id
                    for execution_id in unique_execution_ids
                    if execution_id not in terminal
                ]
                traces = (
                    await self.repository.load_agent_execution_traces(
                        pending_ids
                    )
                )
                missing = [
                    execution_id
                    for execution_id in pending_ids
                    if execution_id not in traces
                ]
                if missing:
                    raise DatabaseError(
                        "One or more agent executions disappeared"
                    )
                for execution_id, trace in traces.items():
                    if trace.job.status in {
                        "COMPLETED",
                        "DEAD_LETTER",
                    }:
                        terminal[execution_id] = trace
                pending_ids = [
                    execution_id
                    for execution_id in pending_ids
                    if execution_id not in terminal
                ]
                if not pending_ids:
                    break
                if not self.background_worker_active:
                    fallback = await asyncio.gather(
                        *(
                            self._execute_submitted(
                                traces[execution_id].job
                            )
                            for execution_id in pending_ids
                        )
                    )
                    terminal.update(
                        {
                            trace.job.execution_id: trace
                            for trace in fallback
                        }
                    )
                    break
                waiter_tasks = [
                    asyncio.create_task(waiters[execution_id].wait())
                    for execution_id in pending_ids
                ]
                try:
                    await asyncio.wait(
                        waiter_tasks,
                        timeout=float(self._lease_seconds),
                    )
                finally:
                    for task in waiter_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(
                        *waiter_tasks,
                        return_exceptions=True,
                    )
            return [terminal[execution_id] for execution_id in execution_ids]
        finally:
            for execution_id, waiter in waiters.items():
                self._remove_completion_waiter(execution_id, waiter)

    async def execute_claimed(
        self,
        job: AgentExecutionJob,
        *,
        worker_id: str,
    ) -> AgentExecutionJob:
        agent, unavailable_output = self._resolve_agent(job)
        if unavailable_output is not None:
            return await self._record_and_finish(
                job,
                worker_id=worker_id,
                output=unavailable_output,
                retryable=False,
            )
        assert agent is not None
        await self._publish(
            EventTypes.AGENT_STARTED,
            job,
            event_id=agent_lifecycle_event_id(
                job.execution_id,
                EventTypes.AGENT_STARTED,
                job.attempt_count,
            ),
        )
        output, retryable = await self._run_agent(job, agent)
        return await self._record_and_finish(
            job,
            worker_id=worker_id,
            output=output,
            retryable=retryable,
        )

    def _resolve_agent(
        self,
        job: AgentExecutionJob,
    ) -> tuple[BaseAgent | None, AgentOutput | None]:
        try:
            agent = self.registry.get(
                job.agent_name,
                version=job.agent_version,
            )
            registration = agent.registration()
            if registration.definition_hash != job.agent_definition_hash:
                output = AgentOutput(
                    agent_name=job.agent_name,
                    status=AgentStatus.FAILED,
                    signal=Signal.BLOCK if job.critical else Signal.NEUTRAL,
                    confidence=0,
                    reason="Queued agent definition no longer matches registry",
                    warnings=["AGENT_DEFINITION_MISMATCH"],
                )
                return None, output
        except ValidationError:
            output = AgentOutput(
                agent_name=job.agent_name,
                status=AgentStatus.FAILED,
                signal=Signal.BLOCK if job.critical else Signal.NEUTRAL,
                confidence=0,
                reason="Queued agent version is unavailable",
                warnings=["AGENT_VERSION_UNAVAILABLE"],
            )
            return None, output
        return agent, None

    async def _run_agent(
        self,
        job: AgentExecutionJob,
        agent: BaseAgent,
    ) -> tuple[AgentOutput, bool]:
        agent_lock = self._agent_locks.setdefault(
            job.agent_name,
            asyncio.Lock(),
        )
        async with self._semaphore, agent_lock:
            output = await agent.run(job.input)
        try:
            ensure_payload_has_no_secrets(output.model_dump(mode="json"))
        except ValueError:
            output = AgentOutput(
                agent_name=job.agent_name,
                status=AgentStatus.FAILED,
                signal=Signal.BLOCK if job.critical else Signal.NEUTRAL,
                confidence=0,
                reason="Agent output rejected by payload security policy",
                warnings=["AGENT_OUTPUT_REJECTED"],
            )
            retryable = False
        else:
            retryable = output.status in {
                AgentStatus.FAILED,
                AgentStatus.TIMEOUT,
            }
        return output, retryable

    async def execute_claimed_many(
        self,
        jobs: list[AgentExecutionJob],
        *,
        worker_id: str,
    ) -> list[AgentExecutionJob]:
        """Run and durably acknowledge a small leased job cohort."""

        if not jobs:
            return []
        execution_ids = [job.execution_id for job in jobs]
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("Claimed batch execution_ids must be unique")

        resolved = [
            (job, *self._resolve_agent(job))
            for job in jobs
        ]
        runnable = [
            (job, agent)
            for job, agent, unavailable_output in resolved
            if agent is not None and unavailable_output is None
        ]
        await self._publish_many(
            [
                self._event_publication(
                    EventTypes.AGENT_STARTED,
                    job,
                    event_id=agent_lifecycle_event_id(
                        job.execution_id,
                        EventTypes.AGENT_STARTED,
                        job.attempt_count,
                    ),
                )
                for job, _ in runnable
            ]
        )
        run_results = await asyncio.gather(
            *(
                self._run_agent(job, agent)
                for job, agent in runnable
            )
        )
        results_by_id = {
            job.execution_id: result
            for (job, _), result in zip(
                runnable,
                run_results,
                strict=True,
            )
        }
        finishes: list[AgentExecutionFinish] = []
        for job, _, unavailable_output in resolved:
            if unavailable_output is not None:
                output = unavailable_output
                retryable = False
            else:
                output, retryable = results_by_id[job.execution_id]
            finishes.append(
                self._prepare_finish(
                    job,
                    worker_id=worker_id,
                    output=output,
                    retryable=retryable,
                )
            )
        updated_jobs = await self.repository.finish_agent_executions(
            finishes
        )
        for updated in updated_jobs:
            if updated.status in {"COMPLETED", "DEAD_LETTER"}:
                self._notify_completion(updated.execution_id)
        await self._publish_many(
            [
                self._terminal_publication(updated)
                for updated in updated_jobs
            ]
        )
        return updated_jobs

    async def _record_and_finish(
        self,
        job: AgentExecutionJob,
        *,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
    ) -> AgentExecutionJob:
        finish = self._prepare_finish(
            job,
            worker_id=worker_id,
            output=output,
            retryable=retryable,
        )
        updated = await self.repository.finish_agent_execution(
            attempt=finish.attempt,
            attempt_memory=finish.attempt_memory,
            worker_id=finish.worker_id,
            output=finish.output,
            retryable=finish.retryable,
            retry_delay_seconds=finish.retry_delay_seconds,
            terminal_memory=finish.terminal_memory,
        )
        if updated.status in {"COMPLETED", "DEAD_LETTER"}:
            self._notify_completion(updated.execution_id)
        publication = self._terminal_publication(updated)
        await self._publish(
            publication.event_type,
            updated,
            event_id=publication.event_id,
        )
        return updated

    def _prepare_finish(
        self,
        job: AgentExecutionJob,
        *,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
    ) -> AgentExecutionFinish:
        completed_at = utcnow()
        started_at = job.updated_at
        attempt = AgentExecutionAttempt(
            execution_id=job.execution_id,
            attempt_number=job.attempt_count,
            worker_id=worker_id,
            status=output.status,
            output=output,
            retryable=retryable,
            started_at=started_at,
            completed_at=completed_at,
        )
        attempt_memory = _memory(
            execution_id=job.execution_id,
            sequence=job.attempt_count * 2,
            entry_type="ATTEMPT",
            payload=attempt.model_dump(mode="json"),
        )
        will_retry = retryable and job.attempt_count < job.max_attempts
        retry_delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds
            * (2 ** max(0, job.attempt_count - 1)),
        )
        terminal_type = (
            None
            if will_retry
            else "OUTPUT"
            if output.status not in {
                AgentStatus.FAILED,
                AgentStatus.TIMEOUT,
            }
            else "DEAD_LETTER"
        )
        terminal_memory = (
            _memory(
                execution_id=job.execution_id,
                sequence=job.attempt_count * 2 + 1,
                entry_type=terminal_type,
                payload=output.model_dump(mode="json"),
            )
            if terminal_type is not None
            else None
        )
        return AgentExecutionFinish(
            attempt=attempt,
            attempt_memory=attempt_memory,
            worker_id=worker_id,
            output=output,
            retryable=retryable,
            retry_delay_seconds=retry_delay,
            terminal_memory=terminal_memory,
        )

    def _terminal_publication(
        self,
        job: AgentExecutionJob,
    ) -> EventPublication:
        event_type = (
            EventTypes.AGENT_RETRY_SCHEDULED
            if job.status == "RETRY"
            else EventTypes.AGENT_DEAD_LETTERED
            if job.status == "DEAD_LETTER"
            else EventTypes.AGENT_COMPLETED
        )
        return self._event_publication(
            event_type,
            job,
            event_id=agent_lifecycle_event_id(
                job.execution_id,
                event_type,
                job.attempt_count,
            ),
        )

    async def trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None:
        return await self.repository.load_agent_execution_trace(execution_id)

    async def list_jobs(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentExecutionJob]:
        return await self.repository.list_agent_execution_jobs(limit=limit)

    async def _publish(
        self,
        event_type: str,
        job: AgentExecutionJob,
        *,
        event_id: str,
    ) -> None:
        if self._event_bus is None:
            return
        publication = self._event_publication(
            event_type,
            job,
            event_id=event_id,
        )
        await self._event_bus.publish(
            publication.topic,
            publication.event_type,
            publication.payload,
            source=publication.source,
            correlation_id=publication.correlation_id,
            event_id=publication.event_id,
        )

    async def _publish_many(
        self,
        publications: list[EventPublication],
    ) -> None:
        if self._event_bus is None or not publications:
            return
        await self._event_bus.publish_many(publications)

    @staticmethod
    def _event_publication(
        event_type: str,
        job: AgentExecutionJob,
        *,
        event_id: str,
    ) -> EventPublication:
        payload = {
            "execution_id": job.execution_id,
            "agent_name": job.agent_name,
            "agent_version": job.agent_version,
            "status": job.status,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "decision_role": job.decision_role,
            "execution_mode": job.execution_mode,
        }
        if (
            event_type
            in {
                EventTypes.AGENT_RETRY_SCHEDULED,
                EventTypes.AGENT_DEAD_LETTERED,
                EventTypes.AGENT_COMPLETED,
            }
            and job.output is not None
        ):
            payload["output"] = job.output.model_dump(mode="json")
        return EventPublication(
            topic=(
                Topics.AGENT_REQUESTS
                if event_type == EventTypes.AGENT_REQUESTED
                else Topics.AGENT_OUTPUTS
            ),
            event_type=event_type,
            payload=payload,
            source="AgentRuntime",
            correlation_id=job.correlation_id,
            event_id=event_id,
        )


class AgentRuntimeWorker:
    """Drain durable agent jobs with recoverable leases."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        worker_id: str | None = None,
        poll_interval_seconds: float = 0.25,
        lease_seconds: int = 30,
        max_concurrency: int = 1,
        claim_batch_size: int = 4,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Agent worker poll interval must be positive")
        if lease_seconds < 1:
            raise ValueError("Agent worker lease must be positive")
        if max_concurrency < 1:
            raise ValueError("Agent worker max_concurrency must be positive")
        if not 1 <= claim_batch_size <= 32:
            raise ValueError("Agent worker claim_batch_size must be 1..32")
        self.runtime = runtime
        self.worker_id = worker_id or runtime._worker_id("agent-worker")
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._max_concurrency = max_concurrency
        self._claim_batch_size = claim_batch_size

    async def run_once(self) -> AgentExecutionJob | None:
        jobs = await self.runtime.repository.claim_next_agent_executions(
            worker_id=self.worker_id,
            lease_seconds=self._lease_seconds,
            limit=self._claim_batch_size,
        )
        if not jobs:
            return None
        processed = await self.runtime.execute_claimed_many(
            jobs,
            worker_id=self.worker_id,
        )
        return processed[0]

    async def _run_slot(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except DatabaseError as exc:
                processed = None
                logger.error(
                    "Agent runtime worker database operation failed",
                    event_type="AGENT_WORKER_DATABASE_ERROR",
                    metadata={"error_type": type(exc).__name__},
                )
            if processed is not None:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def run(self, stop_event: asyncio.Event) -> None:
        """Drain jobs with bounded process-local parallelism."""

        self.runtime._background_worker_started()
        try:
            async with asyncio.TaskGroup() as slots:
                for _ in range(self._max_concurrency):
                    slots.create_task(self._run_slot(stop_event))
        finally:
            self.runtime._background_worker_stopped()

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

from app.agents.registry import AgentRegistry
from app.core.errors import DatabaseError, ValidationError
from app.core.event_bus import EventBus, Topics
from app.core.logging import ServiceLogger
from app.core.payload_security import ensure_payload_has_no_secrets
from app.schemas.agents import (
    AgentExecutionAttempt,
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

    async def load_agent_execution_trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None: ...

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

    async def _claim(
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
            return await self._claim(
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
        async with self._lock:
            ordered = sorted(
                self._jobs.values(),
                key=lambda item: (item.available_at, item.created_at),
            )
            for job in ordered:
                claimed = await self._claim(
                    job.execution_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                )
                if claimed is not None:
                    return claimed
            return None

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
        now = utcnow()
        async with self._lock:
            execution_id = attempt.execution_id
            job = self._jobs[execution_id]
            if job.status != "LEASED" or job.leased_by != worker_id:
                raise ValidationError("Agent execution lease ownership lost")
            attempts = self._attempts[execution_id]
            expected = len(attempts) + 1
            if attempt.attempt_number != expected:
                raise ValidationError("Agent attempts must be append-only")
            if attempt_memory.sequence != attempt.attempt_number * 2:
                raise ValidationError("Agent attempt memory sequence is invalid")
            attempts.append(attempt)
            self._memory[execution_id].append(attempt_memory)
            successful = output.status not in {
                AgentStatus.FAILED,
                AgentStatus.TIMEOUT,
            }
            will_retry = (
                not successful
                and retryable
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
                if output.status == AgentStatus.TIMEOUT
                else "AGENT_FAILED"
            )
            updated = job.model_copy(
                update={
                    "status": status,
                    "available_at": (
                        now + timedelta(seconds=retry_delay_seconds)
                        if will_retry
                        else now
                    ),
                    "leased_by": None,
                    "lease_expires_at": None,
                    "last_error_code": error_code,
                    "output": output,
                    "updated_at": now,
                    "completed_at": (
                        now if status in {"COMPLETED", "DEAD_LETTER"} else None
                    ),
                }
            )
            self._jobs[execution_id] = updated
            if terminal_memory is not None:
                self._memory[execution_id].append(terminal_memory)
            return updated

    async def load_agent_execution_trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None:
        async with self._lock:
            job = self._jobs.get(execution_id)
            if job is None:
                return None
            return AgentExecutionTrace(
                job=job,
                attempts=list(self._attempts[execution_id]),
                memory=list(self._memory[execution_id]),
            )

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
        await asyncio.gather(
            *(
                self._publish_requested_bounded(job)
                for job in jobs
            )
        )
        return jobs

    async def _publish_requested_bounded(
        self,
        job: AgentExecutionJob,
    ) -> None:
        async with self._submission_semaphore:
            await self._publish_requested(job)

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
        traces = await asyncio.gather(
            *(self._execute_submitted(job) for job in jobs)
        )
        outputs: list[AgentOutput] = []
        for trace in traces:
            if trace.job.output is None:
                raise DatabaseError("Terminal agent execution has no output")
            outputs.append(trace.job.output)
        return outputs

    async def execute_claimed(
        self,
        job: AgentExecutionJob,
        *,
        worker_id: str,
    ) -> AgentExecutionJob:
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
                return await self._record_and_finish(
                    job,
                    worker_id=worker_id,
                    output=output,
                    retryable=False,
                )
        except ValidationError:
            output = AgentOutput(
                agent_name=job.agent_name,
                status=AgentStatus.FAILED,
                signal=Signal.BLOCK if job.critical else Signal.NEUTRAL,
                confidence=0,
                reason="Queued agent version is unavailable",
                warnings=["AGENT_VERSION_UNAVAILABLE"],
            )
            return await self._record_and_finish(
                job,
                worker_id=worker_id,
                output=output,
                retryable=False,
            )

        await self._publish(
            EventTypes.AGENT_STARTED,
            job,
            event_id=agent_lifecycle_event_id(
                job.execution_id,
                EventTypes.AGENT_STARTED,
                job.attempt_count,
            ),
        )
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
        return await self._record_and_finish(
            job,
            worker_id=worker_id,
            output=output,
            retryable=retryable,
        )

    async def _record_and_finish(
        self,
        job: AgentExecutionJob,
        *,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
    ) -> AgentExecutionJob:
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
        updated = await self.repository.finish_agent_execution(
            attempt=attempt,
            attempt_memory=attempt_memory,
            worker_id=worker_id,
            output=output,
            retryable=retryable,
            retry_delay_seconds=retry_delay,
            terminal_memory=terminal_memory,
        )
        if updated.status in {"COMPLETED", "DEAD_LETTER"}:
            self._notify_completion(updated.execution_id)
        event_type = (
            EventTypes.AGENT_RETRY_SCHEDULED
            if updated.status == "RETRY"
            else EventTypes.AGENT_DEAD_LETTERED
            if updated.status == "DEAD_LETTER"
            else EventTypes.AGENT_COMPLETED
        )
        await self._publish(
            event_type,
            updated,
            event_id=agent_lifecycle_event_id(
                job.execution_id,
                event_type,
                job.attempt_count,
            ),
        )
        return updated

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
        await self._event_bus.publish(
            Topics.AGENT_REQUESTS
            if event_type == EventTypes.AGENT_REQUESTED
            else Topics.AGENT_OUTPUTS,
            event_type,
            payload,
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
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Agent worker poll interval must be positive")
        if lease_seconds < 1:
            raise ValueError("Agent worker lease must be positive")
        if max_concurrency < 1:
            raise ValueError("Agent worker max_concurrency must be positive")
        self.runtime = runtime
        self.worker_id = worker_id or runtime._worker_id("agent-worker")
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._max_concurrency = max_concurrency

    async def run_once(self) -> AgentExecutionJob | None:
        job = await self.runtime.repository.claim_next_agent_execution(
            worker_id=self.worker_id,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return None
        return await self.runtime.execute_claimed(
            job,
            worker_id=self.worker_id,
        )

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

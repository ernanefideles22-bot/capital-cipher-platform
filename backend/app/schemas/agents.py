"""Versioned contracts for isolated PAPER-only agent execution."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.common import AgentStatus, Signal, utcnow
from app.schemas.events import CONTRACT_VERSION

AgentExecutionStatus = Literal[
    "PENDING",
    "LEASED",
    "RETRY",
    "COMPLETED",
    "DEAD_LETTER",
]
AgentDecisionRole = Literal["PRIMARY", "SHADOW"]
AgentExecutionMode = Literal["PAPER"]


class StrictAgentModel(BaseModel):
    """Reject fields that are not part of the published agent boundary."""

    model_config = ConfigDict(extra="forbid")


class AgentInput(StrictAgentModel):
    """Base input contract for every agent."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        max_length=256,
    )
    correlation_id: str = Field(min_length=1, max_length=36)
    agent_name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]+$",
    )
    timestamp: AwareDatetime = Field(default_factory=utcnow)
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    market_context: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class AgentOutput(StrictAgentModel):
    """Stable output contract shared by every registered agent."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    agent_name: str = Field(min_length=1)
    status: AgentStatus
    signal: Signal
    confidence: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=1_000)
    evidence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0, default=0)
    created_at: AwareDatetime = Field(default_factory=utcnow)


class AgentRegistration(StrictAgentModel):
    """Immutable active definition exposed by the runtime registry."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    registry_version: Literal["agent-registry-v1"] = "agent-registry-v1"
    agent_name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]+$",
    )
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    description: str = ""
    required_inputs: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    input_contract: Literal["AgentInputV1"] = "AgentInputV1"
    output_contract: Literal["AgentOutputV1"] = "AgentOutputV1"
    execution_mode: AgentExecutionMode = "PAPER"
    decision_role: AgentDecisionRole = "SHADOW"
    critical: bool = False
    timeout_ms: int = Field(default=5000, ge=1, le=300_000)
    max_attempts: int = Field(default=3, ge=1, le=10)
    enabled: bool = True
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def agent_key(self) -> str:
        return f"{self.agent_name}@{self.version}"


class AgentHealth(StrictAgentModel):
    name: str
    status: str
    version: str
    critical: bool
    enabled: bool
    last_run_at: AwareDatetime | None = None
    last_failure_at: AwareDatetime | None = None
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0
    total_runs: int = 0
    total_failures: int = 0
    last_signal: str | None = None
    last_confidence: int | None = None


class AgentExecutionRequest(StrictAgentModel):
    """Validated request accepted by the registry-backed runtime."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    runtime_version: Literal["agent-runtime-v1"] = "agent-runtime-v1"
    execution_mode: AgentExecutionMode = "PAPER"
    agent_version: str = Field(
        default="1.0.0",
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )
    input: AgentInput

    @model_validator(mode="after")
    def validate_paper_request(self) -> "AgentExecutionRequest":
        if not self.idempotency_key:
            self.idempotency_key = self.input.request_id
        return self

    @property
    def agent_key(self) -> str:
        return f"{self.input.agent_name}@{self.agent_version}"


def agent_execution_fingerprint(request: AgentExecutionRequest) -> str:
    """Stable execution identity for idempotent submission and replay."""

    payload = {
        "runtime_version": request.runtime_version,
        "execution_mode": request.execution_mode,
        "agent_key": request.agent_key,
        "idempotency_key": request.idempotency_key,
        "input": request.input.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AgentExecutionAttempt(StrictAgentModel):
    """Append-only evidence for one bounded execution attempt."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    execution_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_number: int = Field(ge=1, le=10)
    worker_id: str = Field(min_length=1, max_length=128)
    status: AgentStatus
    output: AgentOutput
    retryable: bool
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_attempt(self) -> "AgentExecutionAttempt":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        if self.output.status != self.status:
            raise ValueError("attempt status must match output status")
        return self


class AgentMemoryEntry(StrictAgentModel):
    """Append-only, execution-scoped memory entry owned by the runtime."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    execution_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    sequence: int = Field(ge=1)
    entry_type: Literal[
        "INPUT",
        "ATTEMPT",
        "OUTPUT",
        "DEAD_LETTER",
    ]
    payload: dict[str, Any]
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: AwareDatetime = Field(default_factory=utcnow)


class AgentExecutionFinish(StrictAgentModel):
    """Validated evidence required to finish one leased execution."""

    attempt: AgentExecutionAttempt
    attempt_memory: AgentMemoryEntry
    worker_id: str = Field(min_length=1, max_length=128)
    output: AgentOutput
    retryable: bool
    retry_delay_seconds: float = Field(ge=0)
    terminal_memory: AgentMemoryEntry | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "AgentExecutionFinish":
        execution_id = self.attempt.execution_id
        if (
            self.attempt_memory.execution_id != execution_id
            or self.attempt.output != self.output
            or self.attempt.worker_id != self.worker_id
            or self.attempt_memory.sequence
            != self.attempt.attempt_number * 2
            or self.attempt_memory.entry_type != "ATTEMPT"
        ):
            raise ValueError("Agent attempt evidence is inconsistent")
        if self.terminal_memory is not None:
            expected_type = (
                "DEAD_LETTER"
                if self.output.status
                in {AgentStatus.FAILED, AgentStatus.TIMEOUT}
                else "OUTPUT"
            )
            if (
                self.terminal_memory.execution_id != execution_id
                or self.terminal_memory.sequence
                != self.attempt.attempt_number * 2 + 1
                or self.terminal_memory.entry_type != expected_type
            ):
                raise ValueError("Terminal agent memory is inconsistent")
        elif self.output.status not in {
            AgentStatus.FAILED,
            AgentStatus.TIMEOUT,
        }:
            raise ValueError("Successful execution requires terminal memory")
        return self


class AgentExecutionJob(StrictAgentModel):
    """Durable queue state; only the runtime may mutate lifecycle fields."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    runtime_version: Literal["agent-runtime-v1"] = "agent-runtime-v1"
    execution_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    correlation_id: str = Field(min_length=1, max_length=36)
    agent_name: str
    agent_version: str
    agent_definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_mode: AgentExecutionMode = "PAPER"
    decision_role: AgentDecisionRole
    critical: bool
    input: AgentInput
    status: AgentExecutionStatus = "PENDING"
    attempt_count: int = Field(default=0, ge=0, le=10)
    max_attempts: int = Field(ge=1, le=10)
    available_at: AwareDatetime
    leased_by: str | None = Field(default=None, max_length=128)
    lease_expires_at: AwareDatetime | None = None
    last_error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]+$",
    )
    output: AgentOutput | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_job_state(self) -> "AgentExecutionJob":
        if self.execution_id != self.request_fingerprint:
            raise ValueError("execution_id must equal request_fingerprint")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count must not exceed max_attempts")
        if self.input.agent_name != self.agent_name:
            raise ValueError("job agent must match input agent")
        leased = self.status == "LEASED"
        if leased != (
            self.leased_by is not None and self.lease_expires_at is not None
        ):
            raise ValueError("lease fields must match LEASED status")
        if leased and self.attempt_count < 1:
            raise ValueError("leased jobs require an active attempt")
        terminal = self.status in {"COMPLETED", "DEAD_LETTER"}
        if terminal != (self.completed_at is not None):
            raise ValueError("terminal jobs require completed_at")
        if terminal and self.output is None:
            raise ValueError("terminal jobs require output")
        if self.output is not None and self.output.agent_name != self.agent_name:
            raise ValueError("job output must match job agent")
        return self


class AgentExecutionTrace(StrictAgentModel):
    """Complete reconstruction of one isolated agent execution."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    job: AgentExecutionJob
    attempts: list[AgentExecutionAttempt]
    memory: list[AgentMemoryEntry]

    @model_validator(mode="after")
    def validate_trace(self) -> "AgentExecutionTrace":
        execution_id = self.job.execution_id
        if any(
            attempt.execution_id != execution_id
            for attempt in self.attempts
        ):
            raise ValueError("trace attempts must match the job execution")
        attempt_numbers = [
            attempt.attempt_number for attempt in self.attempts
        ]
        if attempt_numbers != list(range(1, len(self.attempts) + 1)):
            raise ValueError("trace attempts must be ordered and contiguous")
        if len(self.attempts) > self.job.attempt_count:
            raise ValueError("trace has more attempts than the job")
        if any(
            entry.execution_id != execution_id
            for entry in self.memory
        ):
            raise ValueError("trace memory must match the job execution")
        memory_sequences = [entry.sequence for entry in self.memory]
        if memory_sequences != sorted(set(memory_sequences)):
            raise ValueError("trace memory sequences must be ordered and unique")
        return self

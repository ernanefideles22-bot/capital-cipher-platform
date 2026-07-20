"""BaseAgent with lifecycle, contract enforcement and timeout (docs/28, docs/11).

Rules enforced here:
- every execution receives a validated AgentInput and returns AgentOutput;
- timeout produces status TIMEOUT and never a free-form response;
- failures produce status FAILED, preserve correlation_id, never crash callers;
- agents never talk to each other directly (isolation via orchestrator).
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Literal

from app.core.logging import ServiceLogger
from app.schemas.agents import AgentHealth, AgentInput, AgentOutput, AgentRegistration
from app.schemas.common import AgentStatus, Signal


class BaseAgent(abc.ABC):
    """Governed base class for PAPER-only runtime agents."""

    name: str = "BaseAgent"
    version: str = "1.0.0"
    description: str = ""
    required_inputs: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    decision_role: Literal["PRIMARY", "SHADOW"] = "SHADOW"
    critical: bool = False
    timeout_ms: int = 5000
    max_attempts: int = 3

    def __init__(self) -> None:
        self.enabled: bool = True
        self.status: str = "REGISTERED"
        self.last_run_at: datetime | None = None
        self.last_failure_at: datetime | None = None
        self.total_runs: int = 0
        self.total_failures: int = 0
        self.total_latency_ms: float = 0.0
        self.last_signal: str | None = None
        self.last_confidence: int | None = None
        self.last_output: AgentOutput | None = None
        self.last_input: AgentInput | None = None
        self._logger = ServiceLogger(self.name)

    # -- lifecycle -----------------------------------------------------------
    def _definition_hash(self) -> str:
        payload = {
            "registry_version": "agent-registry-v1",
            "agent_name": self.name,
            "version": self.version,
            "description": self.description,
            "required_inputs": sorted(self.required_inputs),
            "capabilities": sorted(self.capabilities),
            "input_contract": "AgentInputV1",
            "output_contract": "AgentOutputV1",
            "execution_mode": "PAPER",
            "decision_role": self.decision_role,
            "critical": self.critical,
            "timeout_ms": self.timeout_ms,
            "max_attempts": self.max_attempts,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def registration(self) -> AgentRegistration:
        return AgentRegistration(
            agent_name=self.name,
            version=self.version,
            description=self.description,
            required_inputs=list(self.required_inputs),
            capabilities=list(self.capabilities),
            decision_role=self.decision_role,
            critical=self.critical,
            timeout_ms=self.timeout_ms,
            max_attempts=self.max_attempts,
            enabled=self.enabled,
            definition_hash=self._definition_hash(),
        )

    async def initialize(self) -> None:
        if not self.enabled:
            self.status = "DISABLED"
            return
        self.status = "INITIALIZING"
        await self._setup()
        self.status = "READY"

    def disable(self) -> None:
        self.enabled = False
        self.status = "DISABLED"

    def enable(self) -> None:
        self.enabled = True
        self.status = "REGISTERED"

    async def _setup(self) -> None:
        """Override for agent-specific initialization."""

    def health(self) -> AgentHealth:
        avg_latency = self.total_latency_ms / self.total_runs if self.total_runs else 0.0
        error_rate = self.total_failures / self.total_runs if self.total_runs else 0.0
        return AgentHealth(
            name=self.name,
            status=self.status,
            version=self.version,
            critical=self.critical,
            enabled=self.enabled,
            last_run_at=self.last_run_at,
            last_failure_at=self.last_failure_at,
            avg_latency_ms=round(avg_latency, 2),
            error_rate=round(error_rate, 4),
            total_runs=self.total_runs,
            total_failures=self.total_failures,
            last_signal=self.last_signal,
            last_confidence=self.last_confidence,
        )

    # -- execution -----------------------------------------------------------
    async def run(self, agent_input: AgentInput) -> AgentOutput:
        """Execute the agent with timeout and contract enforcement."""
        if not self.enabled:
            return self._output(
                AgentStatus.SKIPPED, Signal.NEUTRAL, 0, "Agent disabled", latency_ms=0
            )
        self.status = "RUNNING"
        self.total_runs += 1
        self.last_run_at = datetime.now(timezone.utc)
        self.last_input = agent_input
        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                self._analyze(agent_input), timeout=self.timeout_ms / 1000
            )
            if output.agent_name != self.name:
                raise ValueError(
                    "Agent output name does not match the registered agent"
                )
            latency_ms = int((time.monotonic() - started) * 1000)
            output = output.model_copy(update={"latency_ms": latency_ms})
            self.status = "READY"
            self.last_signal = output.signal.value
            self.last_confidence = output.confidence
            self.last_output = output
            self.total_latency_ms += latency_ms
            self._logger.info(
                f"{self.name} completed",
                event_type="AGENT_COMPLETED",
                correlation_id=agent_input.correlation_id,
                metadata={"signal": output.signal.value, "confidence": output.confidence},
            )
            return output
        except asyncio.TimeoutError:
            self.status = "TIMEOUT"
            self.total_failures += 1
            self.last_failure_at = datetime.now(timezone.utc)
            latency_ms = int((time.monotonic() - started) * 1000)
            self._logger.error(
                f"{self.name} timeout",
                event_type="AGENT_TIMEOUT",
                correlation_id=agent_input.correlation_id,
            )
            output = self._output(
                AgentStatus.TIMEOUT,
                Signal.BLOCK if self.critical else Signal.NEUTRAL,
                0,
                f"{self.name} exceeded timeout of {self.timeout_ms}ms",
                latency_ms=latency_ms,
            )
            self.last_output = output
            return output
        except Exception as exc:
            self.status = "FAILED"
            self.total_failures += 1
            self.last_failure_at = datetime.now(timezone.utc)
            latency_ms = int((time.monotonic() - started) * 1000)
            self._logger.error(
                f"{self.name} failed with {type(exc).__name__}",
                event_type="AGENT_FAILED",
                correlation_id=agent_input.correlation_id,
                metadata={"error_type": type(exc).__name__},
            )
            output = self._output(
                AgentStatus.FAILED,
                Signal.BLOCK if self.critical else Signal.NEUTRAL,
                0,
                f"{self.name} failed with {type(exc).__name__}",
                latency_ms=latency_ms,
            )
            self.last_output = output
            return output

    def _output(
        self,
        status: AgentStatus,
        signal: Signal,
        confidence: int,
        reason: str,
        *,
        evidence: dict | None = None,
        warnings: list[str] | None = None,
        latency_ms: int = 0,
    ) -> AgentOutput:
        return AgentOutput(
            agent_name=self.name,
            status=status,
            signal=signal,
            confidence=confidence,
            reason=reason,
            evidence=evidence or {},
            warnings=warnings or [],
            latency_ms=latency_ms,
        )

    @abc.abstractmethod
    async def _analyze(self, agent_input: AgentInput) -> AgentOutput:
        """Agent-specific analysis. Must return a contract-valid AgentOutput."""

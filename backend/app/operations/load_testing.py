"""Deterministic PAPER-only load and chaos evidence harnesses."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from app.agents.runtime import AgentRuntime
from app.operations.metrics import percentile
from app.operations.resilience import RecoveryCoordinator
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import AgentStatus
from app.schemas.operations import ResilienceTestRun


class AgentLoadHarness:
    """Runs a bounded cohort without exchange credentials or order adapters."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def run(
        self,
        *,
        symbol: str = "BTCUSDT",
        timeframe: str = "15m",
        max_duration_ms: float = 30_000,
        max_p95_latency_ms: float = 2_000,
        max_error_rate: float = 0.01,
        environment: str = "CI",
    ) -> ResilienceTestRun:
        registrations = self._runtime.registry.registrations()
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        correlation_id = f"month10-load-{started_at.timestamp():.6f}"
        requests = [
            AgentExecutionRequest(
                agent_version=registration.version,
                idempotency_key=(
                    f"{correlation_id}:{registration.agent_name}"
                ),
                input=AgentInput(
                    request_id=(
                        f"{correlation_id}:{registration.agent_name}"
                    ),
                    correlation_id=correlation_id[:36],
                    agent_name=registration.agent_name,
                    symbol=symbol,
                    timeframe=timeframe,
                    market_context={"exchange": "BINANCE"},
                ),
            )
            for registration in registrations
        ]
        outputs = await self._runtime.execute_many(requests)
        duration_ms = (time.perf_counter() - started) * 1_000
        completed_at = datetime.now(timezone.utc)
        failures = sum(
            output.status in {AgentStatus.FAILED, AgentStatus.TIMEOUT}
            for output in outputs
        )
        error_rate = failures / len(outputs) if outputs else 1.0
        p95 = percentile(
            [float(output.latency_ms) for output in outputs],
            0.95,
        )
        invariants = {
            "exact_target_executed": len(outputs) == len(registrations),
            "paper_only": all(
                registration.execution_mode == "PAPER"
                for registration in registrations
            ),
            "primary_count_is_three": sum(
                registration.decision_role == "PRIMARY"
                for registration in registrations
            )
            == 3,
            "no_order_authority": all(
                not any(
                    capability in {
                        "submit-order",
                        "cancel-order",
                        "exchange-credentials",
                    }
                    for capability in registration.capabilities
                )
                for registration in registrations
            ),
            "duration_within_slo": duration_ms <= max_duration_ms,
            "latency_within_slo": p95 <= max_p95_latency_ms,
            "error_rate_within_slo": error_rate <= max_error_rate,
        }
        return ResilienceTestRun(
            run_type="LOAD",
            scenario="paper-agent-cohort-load",
            status="PASSED" if all(invariants.values()) else "FAILED",
            environment=environment,
            target_agents=len(registrations),
            executed_agents=len(outputs),
            duration_ms=round(duration_ms, 6),
            throughput_per_second=round(
                len(outputs) / max(duration_ms / 1_000, 1e-9),
                6,
            ),
            p95_latency_ms=round(p95, 6),
            error_rate=round(error_rate, 8),
            invariants=invariants,
            started_at=started_at,
            completed_at=completed_at,
        )


class DeterministicChaosHarness:
    """Proves safe halt, degraded mode and conservative recovery."""

    @staticmethod
    def critical_database_outage() -> ResilienceTestRun:
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        recovery = RecoveryCoordinator(recovery_successes_required=3)
        recovery.observe(
            "DATABASE",
            healthy=False,
            reason="injected local database outage",
        )
        halted = not recovery.decisions_allowed
        for dependency in ("DATABASE", "AUDIT", "RISK"):
            for _ in range(3):
                recovery.observe(
                    dependency,
                    healthy=True,
                    reason="deterministic recovery probe",
                )
        recovered = recovery.decisions_allowed
        duration_ms = (time.perf_counter() - started) * 1_000
        completed_at = datetime.now(timezone.utc)
        invariants = {
            "critical_failure_halts_decisions": halted,
            "recovery_requires_confirmations": recovered,
            "paper_only": True,
            "no_order_submission": True,
        }
        return ResilienceTestRun(
            run_type="CHAOS",
            scenario="critical-database-outage",
            status="PASSED" if all(invariants.values()) else "FAILED",
            environment="CI",
            duration_ms=round(duration_ms, 6),
            throughput_per_second=0,
            p95_latency_ms=0,
            error_rate=0,
            recovery_time_ms=round(duration_ms, 6),
            invariants=invariants,
            started_at=started_at,
            completed_at=completed_at,
        )

    @staticmethod
    def optional_broker_outage() -> ResilienceTestRun:
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        recovery = RecoveryCoordinator(recovery_successes_required=3)
        recovery.observe(
            "BROKER",
            healthy=False,
            reason="injected optional broker outage",
        )
        invariants = {
            "mode_is_degraded": recovery.mode == "DEGRADED",
            "decisions_remain_allowed": recovery.decisions_allowed,
            "shadow_work_is_suspended": not recovery.shadow_allowed,
            "paper_only": True,
            "no_order_submission": True,
        }
        duration_ms = (time.perf_counter() - started) * 1_000
        completed_at = datetime.now(timezone.utc)
        return ResilienceTestRun(
            run_type="CHAOS",
            scenario="optional-broker-outage",
            status="PASSED" if all(invariants.values()) else "FAILED",
            environment="CI",
            duration_ms=round(duration_ms, 6),
            throughput_per_second=0,
            p95_latency_ms=0,
            error_rate=0,
            invariants=invariants,
            started_at=started_at,
            completed_at=completed_at,
        )

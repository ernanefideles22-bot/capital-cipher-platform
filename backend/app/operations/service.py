"""Operational metrics, SLOs, alerts and cost admission control."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date
from typing import Awaitable, Callable, Protocol

from app.agents.registry import AgentRegistry
from app.core.logging import ServiceLogger
from app.operations.metrics import BoundedMetricRegistry
from app.operations.resilience import DependencyName, RecoveryCoordinator
from app.schemas.agents import AgentOutput, AgentRegistration
from app.schemas.common import AgentStatus, utcnow
from app.schemas.operations import (
    CostBudgetStatus,
    CostUsageRecord,
    OperationalAlertEvent,
    OperationalMetricSnapshot,
    ResilienceTestRun,
    SLOEvaluation,
)

logger = ServiceLogger("operations")


class OperationalRepository(Protocol):
    async def save_operational_metric_snapshot(
        self,
        snapshot: OperationalMetricSnapshot,
    ) -> OperationalMetricSnapshot: ...

    async def list_operational_metric_snapshots(
        self,
        *,
        limit: int = 100,
    ) -> list[OperationalMetricSnapshot]: ...

    async def save_slo_evaluations(
        self,
        evaluations: list[SLOEvaluation],
    ) -> list[SLOEvaluation]: ...

    async def list_slo_evaluations(
        self,
        *,
        limit: int = 100,
    ) -> list[SLOEvaluation]: ...

    async def save_operational_alert_event(
        self,
        event: OperationalAlertEvent,
    ) -> OperationalAlertEvent: ...

    async def list_operational_alert_events(
        self,
        *,
        limit: int = 100,
    ) -> list[OperationalAlertEvent]: ...

    async def save_cost_usage_record(
        self,
        record: CostUsageRecord,
    ) -> CostUsageRecord: ...

    async def list_cost_usage_records(
        self,
        *,
        limit: int = 100,
    ) -> list[CostUsageRecord]: ...

    async def save_resilience_test_run(
        self,
        run: ResilienceTestRun,
    ) -> ResilienceTestRun: ...

    async def list_resilience_test_runs(
        self,
        *,
        limit: int = 100,
    ) -> list[ResilienceTestRun]: ...


ProbeCallback = Callable[[], Awaitable[dict[DependencyName, tuple[bool, str]]]]


class OperationsService:
    """Single operational boundary; it has no order or risk authority."""

    def __init__(
        self,
        registry: AgentRegistry,
        *,
        repository: OperationalRepository | None = None,
        metric_capacity: int = 10_000,
        window_seconds: int = 300,
        daily_budget_usd: float = 10.0,
        budget_warning_percent: float = 80.0,
        agent_execution_unit_cost_usd: float = 0.0,
        agent_success_target: float = 0.99,
        agent_p95_latency_target_ms: float = 2_000,
        orchestrator_success_target: float = 0.99,
        orchestrator_p95_latency_target_ms: float = 5_000,
        recovery_successes_required: int = 3,
    ) -> None:
        if not 10 <= window_seconds <= 86_400:
            raise ValueError("Operational window must be 10..86400 seconds")
        if daily_budget_usd <= 0:
            raise ValueError("Daily operational budget must be positive")
        if not 1 <= budget_warning_percent < 100:
            raise ValueError("Budget warning percent must be 1..<100")
        if agent_execution_unit_cost_usd < 0:
            raise ValueError("Agent execution unit cost cannot be negative")
        for target in (agent_success_target, orchestrator_success_target):
            if not 0.5 <= target <= 1:
                raise ValueError("Success SLO target must be 0.5..1")
        self.registry = registry
        self.repository = repository
        self.metrics = BoundedMetricRegistry(
            max_samples_per_metric=metric_capacity
        )
        self.recovery = RecoveryCoordinator(
            recovery_successes_required=recovery_successes_required
        )
        self._window_seconds = window_seconds
        self._daily_budget_usd = daily_budget_usd
        self._budget_warning_percent = budget_warning_percent
        self._agent_unit_cost = agent_execution_unit_cost_usd
        self._agent_success_target = agent_success_target
        self._agent_latency_target = agent_p95_latency_target_ms
        self._orchestrator_success_target = orchestrator_success_target
        self._orchestrator_latency_target = (
            orchestrator_p95_latency_target_ms
        )
        self._snapshots: deque[OperationalMetricSnapshot] = deque(maxlen=1_000)
        self._slo_evaluations: deque[SLOEvaluation] = deque(maxlen=5_000)
        self._alert_events: deque[OperationalAlertEvent] = deque(maxlen=5_000)
        self._cost_records: deque[CostUsageRecord] = deque(maxlen=100_000)
        self._resilience_runs: deque[ResilienceTestRun] = deque(maxlen=1_000)
        self._active_alerts: dict[str, OperationalAlertEvent] = {}
        self._alert_sequences: dict[str, int] = {}
        self._alert_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.repository is None:
            return
        snapshots, slos, alerts, costs, resilience = await asyncio.gather(
            self.repository.list_operational_metric_snapshots(limit=1_000),
            self.repository.list_slo_evaluations(limit=5_000),
            self.repository.list_operational_alert_events(limit=5_000),
            self.repository.list_cost_usage_records(limit=100_000),
            self.repository.list_resilience_test_runs(limit=1_000),
        )
        self._snapshots.extend(reversed(snapshots))
        self._slo_evaluations.extend(reversed(slos))
        self._cost_records.extend(reversed(costs))
        self._resilience_runs.extend(reversed(resilience))
        for event in reversed(alerts):
            self._alert_events.append(event)
            self._alert_sequences[event.alert_key] = max(
                self._alert_sequences.get(event.alert_key, 0),
                event.lifecycle_sequence,
            )
            if event.event_type == "OPENED":
                self._active_alerts[event.alert_key] = event
            else:
                self._active_alerts.pop(event.alert_key, None)

    @property
    def decisions_allowed(self) -> bool:
        return self.recovery.decisions_allowed

    @property
    def shadow_admission_allowed(self) -> bool:
        return (
            self.recovery.shadow_allowed
            and self.budget_status().shadow_admission_allowed
        )

    def admitted_registrations(
        self,
        registrations: list[AgentRegistration],
    ) -> list[AgentRegistration]:
        if self.shadow_admission_allowed:
            return registrations
        return [
            registration
            for registration in registrations
            if registration.decision_role == "PRIMARY"
        ]

    async def observe_agent_batch(
        self,
        outputs: list[AgentOutput],
        *,
        correlation_id: str,
        duration_ms: float,
    ) -> CostUsageRecord | None:
        failures = sum(
            output.status in {AgentStatus.FAILED, AgentStatus.TIMEOUT}
            for output in outputs
        )
        timeouts = sum(
            output.status == AgentStatus.TIMEOUT for output in outputs
        )
        self.metrics.increment("agents.executions", len(outputs))
        self.metrics.increment("agents.failures", failures)
        self.metrics.increment("agents.timeouts", timeouts)
        self.metrics.observe("agent_batch.duration_ms", duration_ms)
        for output in outputs:
            self.metrics.observe(
                "agents.execution_latency_ms",
                output.latency_ms,
            )
        if not outputs:
            return None
        record = CostUsageRecord(
            cost_center="AGENT_RUNTIME",
            resource="paper-agent-execution",
            quantity=float(len(outputs)),
            unit="execution",
            unit_cost_usd=self._agent_unit_cost,
            estimated_cost_usd=(
                float(len(outputs)) * self._agent_unit_cost
            ),
            correlation_id=correlation_id,
        )
        return await self.record_cost(record)

    def observe_orchestrator_cycle(
        self,
        *,
        success: bool,
        duration_ms: float,
    ) -> None:
        self.metrics.increment("orchestrator.cycles")
        if not success:
            self.metrics.increment("orchestrator.failures")
        self.metrics.observe("orchestrator.cycle_latency_ms", duration_ms)

    async def record_cost(
        self,
        record: CostUsageRecord,
    ) -> CostUsageRecord:
        stored = (
            await self.repository.save_cost_usage_record(record)
            if self.repository is not None
            else record
        )
        if all(item.usage_id != stored.usage_id for item in self._cost_records):
            self._cost_records.append(stored)
        status = self.budget_status()
        self.metrics.gauge(
            "cost.daily_utilization_percent",
            status.utilization_percent,
        )
        return stored

    def budget_status(self, *, on_date: date | None = None) -> CostBudgetStatus:
        target_date = on_date or utcnow().date()
        spent = sum(
            record.estimated_cost_usd
            for record in self._cost_records
            if record.observed_at.date() == target_date
        )
        utilization = spent / self._daily_budget_usd * 100
        if utilization >= 100:
            status = "HARD_LIMIT"
        elif utilization >= self._budget_warning_percent:
            status = "WARNING"
        else:
            status = "HEALTHY"
        return CostBudgetStatus(
            daily_budget_usd=self._daily_budget_usd,
            spent_usd=round(spent, 8),
            remaining_usd=round(
                max(0.0, self._daily_budget_usd - spent),
                8,
            ),
            utilization_percent=round(utilization, 6),
            warning_percent=self._budget_warning_percent,
            status=status,
            shadow_admission_allowed=status != "HARD_LIMIT",
        )

    async def capture_snapshot(
        self,
        *,
        correlation_id: str,
        persist: bool = True,
    ) -> OperationalMetricSnapshot:
        registrations = self.registry.registrations()
        snapshot = self.metrics.snapshot(
            correlation_id=correlation_id,
            window_seconds=self._window_seconds,
            registered_agents=len(registrations),
            active_agents=sum(item.enabled for item in registrations),
        )
        stored = (
            await self.repository.save_operational_metric_snapshot(snapshot)
            if persist and self.repository is not None
            else snapshot
        )
        if all(
            item.snapshot_id != stored.snapshot_id for item in self._snapshots
        ):
            self._snapshots.append(stored)
        return stored

    def _rate_evaluation(
        self,
        *,
        name: str,
        total_metric: str,
        failure_metric: str,
        target: float,
    ) -> SLOEvaluation:
        total = int(
            self.metrics.counter(
                total_metric,
                window_seconds=self._window_seconds,
            )
        )
        if total == 0:
            return SLOEvaluation(
                slo_name=name,
                comparator="GTE",
                target=target,
                sample_count=0,
                compliant=None,
                error_budget_remaining_percent=100,
                status="NO_DATA",
                window_seconds=self._window_seconds,
            )
        failures = self.metrics.counter(
            failure_metric,
            window_seconds=self._window_seconds,
        )
        measured = max(0.0, min(1.0, 1 - failures / total))
        allowed_error = max(1e-12, 1 - target)
        consumed = (1 - measured) / allowed_error * 100
        remaining = max(0.0, 100 - consumed)
        compliant = measured >= target
        return SLOEvaluation(
            slo_name=name,
            comparator="GTE",
            target=target,
            measured=round(measured, 8),
            sample_count=total,
            compliant=compliant,
            error_budget_remaining_percent=round(remaining, 6),
            status=(
                "BREACHED"
                if not compliant
                else "WARNING"
                if remaining <= 20
                else "HEALTHY"
            ),
            window_seconds=self._window_seconds,
        )

    def _latency_evaluation(
        self,
        *,
        name: str,
        metric: str,
        target: float,
    ) -> SLOEvaluation:
        summary = self.metrics.summary(
            metric,
            window_seconds=self._window_seconds,
        )
        if summary.count == 0:
            return SLOEvaluation(
                slo_name=name,
                comparator="LTE",
                target=target,
                sample_count=0,
                compliant=None,
                error_budget_remaining_percent=100,
                status="NO_DATA",
                window_seconds=self._window_seconds,
            )
        measured = summary.p95
        compliant = measured <= target
        remaining = max(0.0, (target - measured) / max(target, 1e-12) * 100)
        return SLOEvaluation(
            slo_name=name,
            comparator="LTE",
            target=target,
            measured=round(measured, 8),
            sample_count=summary.count,
            compliant=compliant,
            error_budget_remaining_percent=round(remaining, 6),
            status=(
                "BREACHED"
                if not compliant
                else "WARNING"
                if remaining <= 20
                else "HEALTHY"
            ),
            window_seconds=self._window_seconds,
        )

    async def evaluate_slos(
        self,
        *,
        correlation_id: str,
    ) -> list[SLOEvaluation]:
        evaluations = [
            self._rate_evaluation(
                name="agents.execution_success_rate",
                total_metric="agents.executions",
                failure_metric="agents.failures",
                target=self._agent_success_target,
            ),
            self._latency_evaluation(
                name="agents.execution_p95_latency_ms",
                metric="agents.execution_latency_ms",
                target=self._agent_latency_target,
            ),
            self._rate_evaluation(
                name="orchestrator.cycle_success_rate",
                total_metric="orchestrator.cycles",
                failure_metric="orchestrator.failures",
                target=self._orchestrator_success_target,
            ),
            self._latency_evaluation(
                name="orchestrator.cycle_p95_latency_ms",
                metric="orchestrator.cycle_latency_ms",
                target=self._orchestrator_latency_target,
            ),
        ]
        stored = (
            await self.repository.save_slo_evaluations(evaluations)
            if self.repository is not None
            else evaluations
        )
        known = {item.evaluation_id for item in self._slo_evaluations}
        self._slo_evaluations.extend(
            item for item in stored if item.evaluation_id not in known
        )
        for evaluation in stored:
            await self._sync_slo_alert(
                evaluation,
                correlation_id=correlation_id,
            )
        return stored

    async def _sync_slo_alert(
        self,
        evaluation: SLOEvaluation,
        *,
        correlation_id: str,
    ) -> None:
        alert_key = f"slo:{evaluation.slo_name}"
        active = self._active_alerts.get(alert_key)
        if evaluation.status == "BREACHED" and active is None:
            await self._record_alert(
                alert_key=alert_key,
                event_type="OPENED",
                severity="ERROR",
                source="OperationsService",
                correlation_id=correlation_id,
                reason=(
                    f"SLO {evaluation.slo_name} breached: "
                    f"{evaluation.measured} vs {evaluation.target}"
                ),
                metadata={
                    "evaluation_id": evaluation.evaluation_id,
                    "measured": evaluation.measured,
                    "target": evaluation.target,
                },
            )
        elif (
            evaluation.status in {"HEALTHY", "WARNING"}
            and active is not None
        ):
            await self._record_alert(
                alert_key=alert_key,
                event_type="RESOLVED",
                severity=active.severity,
                source="OperationsService",
                correlation_id=correlation_id,
                reason=f"SLO {evaluation.slo_name} recovered",
                metadata={"evaluation_id": evaluation.evaluation_id},
            )

    async def _record_alert(
        self,
        *,
        alert_key: str,
        event_type: str,
        severity: str,
        source: str,
        correlation_id: str | None,
        reason: str,
        metadata: dict,
    ) -> OperationalAlertEvent | None:
        async with self._alert_lock:
            active = self._active_alerts.get(alert_key)
            if event_type == "OPENED" and active is not None:
                return active
            if event_type == "RESOLVED" and active is None:
                return None
            sequence = self._alert_sequences.get(alert_key, 0) + 1
            event = OperationalAlertEvent(
                alert_key=alert_key,
                lifecycle_sequence=sequence,
                event_type=event_type,
                severity=severity,
                source=source,
                correlation_id=correlation_id,
                reason=reason,
                metadata=metadata,
            )
            stored = (
                await self.repository.save_operational_alert_event(event)
                if self.repository is not None
                else event
            )
            if all(
                item.alert_event_id != stored.alert_event_id
                for item in self._alert_events
            ):
                self._alert_events.append(stored)
            self._alert_sequences[alert_key] = sequence
            if event_type == "OPENED":
                self._active_alerts[alert_key] = stored
            else:
                self._active_alerts.pop(alert_key, None)
            return stored

    async def observe_dependency(
        self,
        name: DependencyName,
        *,
        healthy: bool,
        reason: str,
        correlation_id: str | None = None,
    ) -> None:
        before = self.recovery.mode
        state = self.recovery.observe(name, healthy=healthy, reason=reason)
        self.metrics.gauge(
            f"dependency.{name.lower()}.healthy",
            1 if state.healthy else 0,
        )
        alert_key = f"dependency:{name.lower()}"
        if not healthy and alert_key not in self._active_alerts:
            await self._record_alert(
                alert_key=alert_key,
                event_type="OPENED",
                severity="CRITICAL" if state.critical else "WARNING",
                source="RecoveryCoordinator",
                correlation_id=correlation_id,
                reason=reason,
                metadata={"dependency": name, "critical": state.critical},
            )
        elif (
            healthy
            and alert_key in self._active_alerts
            and (
                not state.critical
                or state.consecutive_successes
                >= self.recovery.recovery_successes_required
            )
        ):
            await self._record_alert(
                alert_key=alert_key,
                event_type="RESOLVED",
                severity=self._active_alerts[alert_key].severity,
                source="RecoveryCoordinator",
                correlation_id=correlation_id,
                reason=f"{name} recovered",
                metadata={"dependency": name, "mode_before": before},
            )

    async def record_resilience_run(
        self,
        run: ResilienceTestRun,
    ) -> ResilienceTestRun:
        stored = (
            await self.repository.save_resilience_test_run(run)
            if self.repository is not None
            else run
        )
        if all(item.run_id != stored.run_id for item in self._resilience_runs):
            self._resilience_runs.append(stored)
        return stored

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        probe: ProbeCallback,
        interval_seconds: float,
    ) -> None:
        if not 1 <= interval_seconds <= 3_600:
            raise ValueError("Operations monitor interval must be 1..3600")
        while not stop_event.is_set():
            try:
                observations = await probe()
                for name, (healthy, reason) in observations.items():
                    await self.observe_dependency(
                        name,
                        healthy=healthy,
                        reason=reason,
                    )
                correlation_id = f"operations-{int(utcnow().timestamp())}"
                await self.capture_snapshot(correlation_id=correlation_id)
                await self.evaluate_slos(correlation_id=correlation_id)
            except Exception as exc:
                logger.error(
                    "Operational monitor cycle failed",
                    event_type="OPERATIONS_MONITOR_FAILED",
                    metadata={"error_type": type(exc).__name__},
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval_seconds,
                )
            except TimeoutError:
                continue

    def status(self) -> dict:
        return {
            "recovery": self.recovery.snapshot(),
            "budget": self.budget_status().model_dump(mode="json"),
            "registered_agents": len(self.registry.registrations()),
            "shadow_admission_allowed": self.shadow_admission_allowed,
            "decisions_allowed": self.decisions_allowed,
            "active_alerts": len(self._active_alerts),
        }

    def snapshots(self, *, limit: int = 100) -> list[OperationalMetricSnapshot]:
        return list(reversed(self._snapshots))[:limit]

    def slo_evaluations(self, *, limit: int = 100) -> list[SLOEvaluation]:
        return list(reversed(self._slo_evaluations))[:limit]

    def alert_events(self, *, limit: int = 100) -> list[OperationalAlertEvent]:
        return list(reversed(self._alert_events))[:limit]

    def active_alerts(self) -> list[OperationalAlertEvent]:
        return sorted(
            self._active_alerts.values(),
            key=lambda item: (item.severity, item.alert_key),
        )

    def cost_records(self, *, limit: int = 100) -> list[CostUsageRecord]:
        return list(reversed(self._cost_records))[:limit]

    def resilience_runs(self, *, limit: int = 100) -> list[ResilienceTestRun]:
        return list(reversed(self._resilience_runs))[:limit]

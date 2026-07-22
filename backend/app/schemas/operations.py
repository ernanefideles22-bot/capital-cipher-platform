"""Strict Month 10 observability, cost and resilience contracts."""

from __future__ import annotations

import math
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.common import utcnow
from app.schemas.events import CONTRACT_VERSION

MetricKind = Literal["COUNTER", "GAUGE", "HISTOGRAM"]
SLOComparator = Literal["GTE", "LTE"]
SLOStatus = Literal["NO_DATA", "HEALTHY", "WARNING", "BREACHED"]
AlertEventType = Literal["OPENED", "RESOLVED"]
AlertSeverity = Literal["WARNING", "ERROR", "CRITICAL"]
BudgetStatus = Literal["HEALTHY", "WARNING", "HARD_LIMIT"]
ResilienceRunType = Literal["LOAD", "CHAOS", "RECOVERY"]
ResilienceRunStatus = Literal["PASSED", "FAILED"]


class StrictOperationalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationalMetricPoint(StrictOperationalModel):
    name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )
    kind: MetricKind
    value: float
    sample_count: int = Field(default=1, ge=0)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_finite_value(self) -> "OperationalMetricPoint":
        if not math.isfinite(self.value):
            raise ValueError("Operational metrics must be finite")
        if len(self.labels) > 16:
            raise ValueError("Operational metric labels are bounded")
        return self


class OperationalMetricSnapshot(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str = Field(min_length=1, max_length=64)
    window_seconds: int = Field(ge=1, le=86_400)
    registered_agents: int = Field(ge=0, le=10_000)
    active_agents: int = Field(ge=0, le=10_000)
    metrics: list[OperationalMetricPoint] = Field(
        min_length=1,
        max_length=512,
    )
    captured_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "OperationalMetricSnapshot":
        if self.active_agents > self.registered_agents:
            raise ValueError("active_agents cannot exceed registered_agents")
        identities = [
            (
                point.name,
                tuple(sorted(point.labels.items())),
            )
            for point in self.metrics
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Metric identities must be unique per snapshot")
        return self


class SLOEvaluation(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    slo_name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )
    comparator: SLOComparator
    target: float = Field(ge=0)
    measured: float | None = Field(default=None, ge=0)
    sample_count: int = Field(ge=0)
    compliant: bool | None = None
    error_budget_remaining_percent: float = Field(ge=0, le=100)
    status: SLOStatus
    window_seconds: int = Field(ge=1, le=86_400)
    evaluated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_evaluation(self) -> "SLOEvaluation":
        no_data = self.status == "NO_DATA"
        if no_data != (
            self.sample_count == 0
            and self.measured is None
            and self.compliant is None
        ):
            raise ValueError("NO_DATA must match empty measurement fields")
        if not no_data and (
            self.sample_count == 0
            or self.measured is None
            or self.compliant is None
        ):
            raise ValueError("Measured SLOs require samples and compliance")
        if self.status == "BREACHED" and self.compliant is not False:
            raise ValueError("BREACHED SLOs must be non-compliant")
        if self.status in {"HEALTHY", "WARNING"} and self.compliant is not True:
            raise ValueError("Healthy/warning SLOs must be compliant")
        return self


class OperationalAlertEvent(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    alert_event_id: str = Field(default_factory=lambda: str(uuid4()))
    alert_key: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9:_.-]+$",
    )
    lifecycle_sequence: int = Field(ge=1)
    event_type: AlertEventType
    severity: AlertSeverity
    source: str = Field(min_length=2, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=64)
    reason: str = Field(min_length=1, max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: AwareDatetime = Field(default_factory=utcnow)


class CostUsageRecord(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    usage_id: str = Field(default_factory=lambda: str(uuid4()))
    cost_center: Literal[
        "AGENT_RUNTIME",
        "EXTERNAL_DATA",
        "STORAGE",
        "OBSERVABILITY",
    ]
    resource: str = Field(
        min_length=2,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    quantity: float = Field(gt=0)
    unit: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    unit_cost_usd: float = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)
    correlation_id: str | None = Field(default=None, max_length=64)
    observed_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_cost_math(self) -> "CostUsageRecord":
        expected = self.quantity * self.unit_cost_usd
        tolerance = max(1e-9, abs(expected) * 1e-6)
        if abs(self.estimated_cost_usd - expected) > tolerance:
            raise ValueError("estimated_cost_usd must equal quantity * unit cost")
        return self


class CostBudgetStatus(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    daily_budget_usd: float = Field(gt=0)
    spent_usd: float = Field(ge=0)
    remaining_usd: float = Field(ge=0)
    utilization_percent: float = Field(ge=0)
    warning_percent: float = Field(gt=0, lt=100)
    status: BudgetStatus
    shadow_admission_allowed: bool
    primary_admission_allowed: Literal[True] = True
    evaluated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_budget(self) -> "CostBudgetStatus":
        expected_remaining = max(0.0, self.daily_budget_usd - self.spent_usd)
        if abs(self.remaining_usd - expected_remaining) > 1e-6:
            raise ValueError("remaining_usd is inconsistent")
        if self.status == "HARD_LIMIT" and self.shadow_admission_allowed:
            raise ValueError("Hard limit must suspend SHADOW admission")
        if self.status != "HARD_LIMIT" and not self.shadow_admission_allowed:
            raise ValueError("Only the hard limit may suspend SHADOW admission")
        return self


class ResilienceTestRun(StrictOperationalModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    run_type: ResilienceRunType
    scenario: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9_.-]+$",
    )
    status: ResilienceRunStatus
    environment: Literal["LOCAL", "CI", "PAPER"]
    target_agents: int = Field(default=0, ge=0, le=10_000)
    executed_agents: int = Field(default=0, ge=0, le=10_000)
    duration_ms: float = Field(ge=0)
    throughput_per_second: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    recovery_time_ms: float | None = Field(default=None, ge=0)
    invariants: dict[str, bool] = Field(min_length=1, max_length=64)
    live_execution_attempted: Literal[False] = False
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_run(self) -> "ResilienceTestRun":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        if self.executed_agents > self.target_agents and self.target_agents:
            raise ValueError("executed_agents cannot exceed target_agents")
        all_invariants = all(self.invariants.values())
        if (self.status == "PASSED") != all_invariants:
            raise ValueError("Run status must match the invariant results")
        return self

"""Versioned contracts for prolonged PAPER shadow validation campaigns."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.common import utcnow

SHADOW_VALIDATION_SCHEMA_VERSION = "1.0.0"
SHADOW_VALIDATION_PROTOCOL_VERSION = "shadow-validation-v1"


def timeframe_seconds(timeframe: str) -> int:
    units = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    return int(timeframe[:-1]) * units[timeframe[-1]]


class StrictShadowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShadowCampaignDefinition(StrictShadowModel):
    schema_version: Literal["1.0.0"] = SHADOW_VALIDATION_SCHEMA_VERSION
    protocol_version: Literal["shadow-validation-v1"] = (
        SHADOW_VALIDATION_PROTOCOL_VERSION
    )
    campaign_id: str = Field(default_factory=lambda: str(uuid4()))
    execution_mode: Literal["PAPER"] = "PAPER"
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(default="15m", pattern=r"^[1-9][0-9]*[mhdw]$")
    replay_start_at: AwareDatetime
    replay_end_at: AwareDatetime
    replay_candle_count: int = Field(ge=673, le=1_000_000)
    checkpoint_interval_candles: int = Field(default=96, ge=1, le=100_000)
    target_registered_agents: Literal[300] = 300
    expected_primary_agents: Literal[3] = 3
    expected_shadow_agents: Literal[297] = 297
    max_error_rate: float = Field(default=0.01, ge=0, le=1)
    max_p95_latency_ms: float = Field(default=2_000, gt=0, le=300_000)
    dataset_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    definition_hash: str = ""
    live_execution_attempted: Literal[False] = False
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_campaign(self) -> "ShadowCampaignDefinition":
        if self.replay_end_at <= self.replay_start_at:
            raise ValueError("Replay end must be after replay start")
        if (self.replay_end_at - self.replay_start_at).total_seconds() < 604_800:
            raise ValueError("Shadow campaign must represent at least seven days")
        expected_duration = (
            self.replay_candle_count - 1
        ) * timeframe_seconds(self.timeframe)
        if (
            self.replay_end_at - self.replay_start_at
        ).total_seconds() != expected_duration:
            raise ValueError("Replay duration does not match candle cadence")
        checkpoint_count = math.ceil(
            self.replay_candle_count / self.checkpoint_interval_candles
        )
        if not 3 <= checkpoint_count <= 1_000:
            raise ValueError("Campaign must contain 3..1000 checkpoints")
        payload = self.model_dump(
            mode="json",
            exclude={"definition_hash", "created_at"},
        )
        expected = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        if self.definition_hash and self.definition_hash != expected:
            raise ValueError("Shadow campaign definition hash mismatch")
        self.definition_hash = expected
        return self


class ShadowCampaignCheckpoint(StrictShadowModel):
    schema_version: Literal["1.0.0"] = SHADOW_VALIDATION_SCHEMA_VERSION
    checkpoint_id: str = Field(default_factory=lambda: str(uuid4()))
    campaign_id: str
    sequence: int = Field(ge=1)
    replay_at: AwareDatetime
    status: Literal[
        "EXECUTED",
        "SUSPENDED_SAFE_DEGRADATION",
        "BLOCKED_RECONCILIATION",
        "BLOCKED_RISK",
    ]
    acceptance_status: Literal["PASSED", "FAILED"]
    recovery_mode: Literal["HEALTHY", "DEGRADED", "SAFE_HALT"]
    degradation_scenario: Literal["BROKER", "DATABASE"] | None = None
    registered_agents: Literal[300] = 300
    primary_agents: Literal[3] = 3
    shadow_agents: Literal[297] = 297
    executed_agents: int = Field(ge=0, le=300)
    failed_agents: int = Field(ge=0, le=300)
    skipped_agents: int = Field(ge=0, le=300)
    duration_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    reconciliation_status: Literal["MATCHED", "DRIFT", "FAILED"]
    reconciliation_mismatches: int = Field(ge=0)
    reconciliation_critical_mismatches: int = Field(ge=0)
    risk_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk_limits_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    order_count: int = Field(ge=0)
    paper_trade_count: int = Field(ge=0)
    invariants: dict[str, bool] = Field(min_length=1, max_length=64)
    order_submission_attempted: Literal[False] = False
    live_execution_attempted: Literal[False] = False
    captured_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> "ShadowCampaignCheckpoint":
        if self.failed_agents + self.skipped_agents > self.executed_agents:
            raise ValueError("Checkpoint output counts are inconsistent")
        if self.status == "EXECUTED" and self.executed_agents != 300:
            raise ValueError("Executed checkpoint must run the exact cohort")
        if self.status != "EXECUTED" and self.executed_agents != 0:
            raise ValueError("Blocked or degraded checkpoint must suspend the cohort")
        if (self.acceptance_status == "PASSED") != all(self.invariants.values()):
            raise ValueError("Checkpoint acceptance must match its invariants")
        return self


class ShadowValidationReport(StrictShadowModel):
    schema_version: Literal["1.0.0"] = SHADOW_VALIDATION_SCHEMA_VERSION
    report_id: str = Field(default_factory=lambda: str(uuid4()))
    campaign: ShadowCampaignDefinition
    status: Literal["PASSED", "FAILED"]
    checkpoint_ids: list[str] = Field(min_length=1, max_length=100_000)
    total_checkpoints: int = Field(ge=1)
    executed_checkpoints: int = Field(ge=0)
    suspended_checkpoints: int = Field(ge=2)
    total_agent_executions: int = Field(ge=0)
    failed_agent_executions: int = Field(ge=0)
    aggregate_error_rate: float = Field(ge=0, le=1)
    max_p95_latency_ms: float = Field(ge=0)
    reconciliation_runs: int = Field(ge=1)
    reconciliation_critical_mismatches: int = Field(ge=0)
    degradation_scenarios: list[Literal["BROKER", "DATABASE"]] = Field(
        min_length=2,
        max_length=2,
    )
    recovery_confirmations: int = Field(ge=3)
    initial_risk_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    final_risk_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    risk_limits_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_order_count: int = Field(ge=0)
    final_order_count: int = Field(ge=0)
    initial_paper_trade_count: int = Field(ge=0)
    final_paper_trade_count: int = Field(ge=0)
    invariants: dict[str, bool] = Field(min_length=1, max_length=64)
    live_execution_attempted: Literal[False] = False
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_report(self) -> "ShadowValidationReport":
        if self.completed_at < self.started_at:
            raise ValueError("Report completion cannot precede its start")
        if self.total_checkpoints != len(self.checkpoint_ids):
            raise ValueError("Checkpoint total does not match checkpoint identities")
        if self.executed_checkpoints + self.suspended_checkpoints != self.total_checkpoints:
            raise ValueError("Checkpoint outcome totals are inconsistent")
        passed = all(self.invariants.values())
        if (self.status == "PASSED") != passed:
            raise ValueError("Report status must match its invariants")
        return self

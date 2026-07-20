"""Contracts for governed specialist evidence and shadow evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.common import Signal, utcnow
from app.schemas.events import CONTRACT_VERSION

SpecialistDomain = Literal[
    "DERIVATIVES",
    "MACRO",
    "ONCHAIN",
    "NEWS",
]
EvaluationStatus = Literal["INSUFFICIENT_SAMPLE", "EVALUATED"]


def _sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StrictEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpecialistEvidence(StrictEvaluationModel):
    """Immutable normalized metric supplied by a governed data adapter."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    evidence_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    domain: SpecialistDomain
    metric_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    scope: str = Field(pattern=r"^(GLOBAL|[A-Z0-9._-]{2,32})$")
    source: str = Field(min_length=2, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=256)
    value: float
    unit: str = Field(min_length=1, max_length=32)
    quality_score: int = Field(ge=0, le=100)
    observed_at: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=utcnow)
    provenance_uri: str | None = Field(default=None, max_length=1_000)
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "SpecialistEvidence":
        if not math.isfinite(self.value):
            raise ValueError("evidence value must be finite")
        if self.received_at < self.observed_at:
            raise ValueError("received_at must not be before observed_at")
        identity = _sha256(
            {
                "schema_version": self.schema_version,
                "domain": self.domain,
                "metric_name": self.metric_name,
                "scope": self.scope,
                "source": self.source,
                "source_event_id": self.source_event_id,
                "value": self.value,
                "unit": self.unit,
                "quality_score": self.quality_score,
                "observed_at": self.observed_at.isoformat(),
                "received_at": self.received_at.isoformat(),
                "provenance_uri": self.provenance_uri,
                "payload_sha256": self.payload_sha256,
            }
        )
        if self.evidence_id and self.evidence_id != identity:
            raise ValueError("evidence_id does not match immutable evidence")
        self.evidence_id = identity
        return self


class AgentForecast(StrictEvaluationModel):
    """Immutable one-horizon forecast derived from a PAPER agent output."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    forecast_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    correlation_id: str = Field(min_length=1, max_length=36)
    agent_name: str = Field(min_length=3, max_length=128)
    agent_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    signal: Signal
    confidence: int = Field(ge=0, le=100)
    probability_up: float = Field(ge=0, le=1)
    reference_price: float = Field(gt=0)
    forecast_at: AwareDatetime
    target_at: AwareDatetime
    horizon_seconds: int = Field(gt=0)
    decision_role: Literal["PRIMARY", "SHADOW"]
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "AgentForecast":
        if self.target_at != self.forecast_at + timedelta(
            seconds=self.horizon_seconds
        ):
            raise ValueError("target_at must equal forecast_at plus horizon")
        identity = _sha256(
            {
                "schema_version": self.schema_version,
                "correlation_id": self.correlation_id,
                "agent_name": self.agent_name,
                "agent_version": self.agent_version,
                "definition_hash": self.definition_hash,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "signal": self.signal.value,
                "confidence": self.confidence,
                "probability_up": self.probability_up,
                "reference_price": self.reference_price,
                "forecast_at": self.forecast_at.isoformat(),
                "target_at": self.target_at.isoformat(),
            }
        )
        if self.forecast_id and self.forecast_id != identity:
            raise ValueError("forecast_id does not match immutable forecast")
        self.forecast_id = identity
        return self


class AgentForecastOutcome(StrictEvaluationModel):
    """Append-only realization and leave-one-out contribution evidence."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    outcome_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    forecast_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    realized_at: AwareDatetime
    realized_price: float = Field(gt=0)
    realized_return: float
    realized_up: float = Field(ge=0, le=1)
    correct: bool | None
    brier_loss: float = Field(ge=0, le=1)
    ensemble_probability_up: float = Field(ge=0, le=1)
    ensemble_brier_loss: float = Field(ge=0, le=1)
    leave_one_out_probability_up: float = Field(ge=0, le=1)
    leave_one_out_brier_loss: float = Field(ge=0, le=1)
    marginal_contribution: float = Field(ge=-1, le=1)
    cohort_size: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "AgentForecastOutcome":
        identity = _sha256(
            {
                "schema_version": self.schema_version,
                "forecast_id": self.forecast_id,
                "realized_at": self.realized_at.isoformat(),
                "realized_price": self.realized_price,
            }
        )
        if self.outcome_id and self.outcome_id != identity:
            raise ValueError("outcome_id does not match immutable outcome")
        self.outcome_id = identity
        return self


class AgentScorecard(StrictEvaluationModel):
    """Read model; Month 8 never feeds this score back into decisions."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    agent_name: str = Field(min_length=3, max_length=128)
    agent_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    sample_count: int = Field(ge=0)
    directional_sample_count: int = Field(ge=0)
    accuracy: float | None = Field(default=None, ge=0, le=1)
    mean_brier_loss: float | None = Field(default=None, ge=0, le=1)
    mean_marginal_contribution: float | None = Field(
        default=None,
        ge=-1,
        le=1,
    )
    status: EvaluationStatus
    minimum_samples: int = Field(default=30, ge=1)
    evaluated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_status(self) -> "AgentScorecard":
        expected = (
            "EVALUATED"
            if self.sample_count >= self.minimum_samples
            else "INSUFFICIENT_SAMPLE"
        )
        if self.status != expected:
            raise ValueError("scorecard status does not match sample count")
        return self

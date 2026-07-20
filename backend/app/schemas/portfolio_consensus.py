"""Month 9 contracts for portfolio construction and decision governance."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.schemas.common import CandidateAction, Signal, utcnow
from app.schemas.events import CONTRACT_VERSION

ExperimentMode = Literal["SHADOW", "CONFIRMATION"]
ExperimentEventType = Literal["CREATED", "ACTIVATED", "RETIRED"]
ConsensusStatus = Literal["INSUFFICIENT_DATA", "READY"]
DriftSeverity = Literal["NONE", "WARNING", "CRITICAL"]
PortfolioProposalStatus = Literal["NO_ACTION", "PROPOSED", "BLOCKED"]


def _sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StrictGovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConsensusExperiment(StrictGovernanceModel):
    """Immutable versioned policy for performance-weighted consensus."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    experiment_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    mode: ExperimentMode = "SHADOW"
    minimum_samples: int = Field(default=100, ge=100, le=100_000)
    minimum_directional_samples: int = Field(default=50, ge=20, le=100_000)
    minimum_accuracy: float = Field(default=0.52, ge=0.5, le=1)
    maximum_brier_loss: float = Field(default=0.24, ge=0, le=0.25)
    minimum_marginal_contribution: float = Field(default=0.0, ge=0, le=1)
    minimum_eligible_agents: int = Field(default=5, ge=3, le=150)
    maximum_agent_weight: float = Field(default=0.25, gt=0, le=0.34)
    buy_probability_threshold: float = Field(default=0.55, gt=0.5, lt=1)
    sell_probability_threshold: float = Field(default=0.45, gt=0, lt=0.5)
    drift_reference_samples: int = Field(default=50, ge=30, le=10_000)
    drift_current_samples: int = Field(default=20, ge=10, le=5_000)
    critical_accuracy_drop: float = Field(default=0.15, gt=0, le=1)
    critical_brier_increase: float = Field(default=0.08, gt=0, le=1)
    critical_marginal_drop: float = Field(default=0.03, gt=0, le=1)
    created_by: str = Field(min_length=2, max_length=128)
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "ConsensusExperiment":
        if self.minimum_directional_samples > self.minimum_samples:
            raise ValueError(
                "minimum_directional_samples cannot exceed minimum_samples"
            )
        if self.sell_probability_threshold >= self.buy_probability_threshold:
            raise ValueError("sell threshold must be below buy threshold")
        if (
            self.minimum_eligible_agents * self.maximum_agent_weight
            < 1 - 1e-9
        ):
            raise ValueError(
                "eligible-agent count and weight cap cannot sum to one"
            )
        identity = _sha256(
            {
                key: value
                for key, value in self.model_dump(mode="json").items()
                if key not in {"experiment_id", "created_at"}
            }
        )
        if self.experiment_id and self.experiment_id != identity:
            raise ValueError("experiment_id does not match immutable definition")
        self.experiment_id = identity
        return self


class ConsensusExperimentEvent(StrictGovernanceModel):
    """Append-only lifecycle event; experiment definitions never mutate."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    event_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    experiment_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_type: ExperimentEventType
    actor: str = Field(min_length=2, max_length=128)
    reason: str = Field(min_length=3, max_length=1_000)
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "ConsensusExperimentEvent":
        identity = _sha256(
            {
                "schema_version": self.schema_version,
                "experiment_id": self.experiment_id,
                "event_type": self.event_type,
                "actor": self.actor,
                "reason": self.reason,
                "created_at": self.created_at.isoformat(),
            }
        )
        if self.event_id and self.event_id != identity:
            raise ValueError("event_id does not match immutable lifecycle event")
        self.event_id = identity
        return self


class ConsensusAgentWeight(StrictGovernanceModel):
    agent_name: str = Field(min_length=3, max_length=128)
    agent_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    sample_count: int = Field(ge=100)
    directional_sample_count: int = Field(ge=20)
    accuracy: float = Field(ge=0, le=1)
    mean_brier_loss: float = Field(ge=0, le=1)
    mean_marginal_contribution: float = Field(ge=-1, le=1)
    probability_up: float = Field(ge=0, le=1)
    raw_score: float = Field(gt=0)
    weight: float = Field(gt=0, le=0.34)


class WeightedConsensus(StrictGovernanceModel):
    """Immutable consensus evidence and conservative overlay result."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    consensus_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    correlation_id: str = Field(min_length=1, max_length=36)
    experiment_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    experiment_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    mode: ExperimentMode
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    status: ConsensusStatus
    baseline_action: CandidateAction
    baseline_confidence: int = Field(ge=0, le=100)
    probability_up: float | None = Field(default=None, ge=0, le=1)
    signal: Signal = Signal.WAIT
    consensus_confidence: int = Field(default=0, ge=0, le=100)
    eligible_agent_count: int = Field(ge=0, le=150)
    excluded_agents: dict[str, str] = Field(default_factory=dict)
    weights: list[ConsensusAgentWeight] = Field(default_factory=list)
    applied: bool = False
    final_action: CandidateAction
    final_confidence: int = Field(ge=0, le=100)
    reason: str = Field(min_length=3, max_length=2_000)
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "WeightedConsensus":
        if self.final_confidence > self.baseline_confidence:
            raise ValueError("consensus overlay cannot increase confidence")
        directional = {CandidateAction.BUY, CandidateAction.SELL}
        if self.baseline_action not in directional:
            if self.final_action != self.baseline_action:
                raise ValueError(
                    "consensus cannot create direction from a non-directional baseline"
                )
        elif self.final_action not in {self.baseline_action, CandidateAction.WAIT}:
            raise ValueError("consensus cannot reverse a primary direction")
        if self.mode == "SHADOW" and (
            self.applied
            or self.final_action != self.baseline_action
            or self.final_confidence != self.baseline_confidence
        ):
            raise ValueError("shadow consensus cannot change the baseline")
        if self.status == "READY":
            if self.probability_up is None or not self.weights:
                raise ValueError("ready consensus requires probability and weights")
            if self.eligible_agent_count != len(self.weights):
                raise ValueError("eligible count must match weight evidence")
            if not math.isclose(
                sum(item.weight for item in self.weights),
                1.0,
                rel_tol=0,
                abs_tol=1e-6,
            ):
                raise ValueError("ready consensus weights must sum to one")
        elif self.probability_up is not None or self.weights:
            raise ValueError(
                "insufficient consensus cannot expose probability or weights"
            )
        identity = _sha256(
            {
                key: value
                for key, value in self.model_dump(mode="json").items()
                if key != "consensus_id"
            }
        )
        if self.consensus_id and self.consensus_id != identity:
            raise ValueError("consensus_id does not match immutable evidence")
        self.consensus_id = identity
        return self


class DriftObservation(StrictGovernanceModel):
    """Rolling out-of-sample degradation evidence for one agent version."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    observation_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    experiment_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent_name: str = Field(min_length=3, max_length=128)
    agent_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    reference_samples: int = Field(ge=30)
    current_samples: int = Field(ge=10)
    reference_accuracy: float | None = Field(default=None, ge=0, le=1)
    current_accuracy: float | None = Field(default=None, ge=0, le=1)
    accuracy_delta: float | None = Field(default=None, ge=-1, le=1)
    reference_brier_loss: float = Field(ge=0, le=1)
    current_brier_loss: float = Field(ge=0, le=1)
    brier_delta: float = Field(ge=-1, le=1)
    reference_marginal_contribution: float = Field(ge=-1, le=1)
    current_marginal_contribution: float = Field(ge=-1, le=1)
    marginal_delta: float = Field(ge=-2, le=2)
    severity: DriftSeverity
    reasons: list[str] = Field(default_factory=list)
    observed_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "DriftObservation":
        if (self.reference_accuracy is None) != (self.current_accuracy is None):
            raise ValueError("accuracy windows must both be available or absent")
        if (self.reference_accuracy is None) != (self.accuracy_delta is None):
            raise ValueError("accuracy_delta availability is inconsistent")
        if self.severity == "NONE" and self.reasons:
            raise ValueError("NONE drift cannot contain alert reasons")
        if self.severity != "NONE" and not self.reasons:
            raise ValueError("drift warning requires at least one reason")
        identity = _sha256(
            {
                key: value
                for key, value in self.model_dump(mode="json").items()
                if key not in {"observation_id", "created_at"}
            }
        )
        if self.observation_id and self.observation_id != identity:
            raise ValueError(
                "observation_id does not match immutable drift evidence"
            )
        self.observation_id = identity
        return self


class PortfolioProposal(StrictGovernanceModel):
    """Advisory target and notional ceiling; the Risk Manager remains final."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    proposal_id: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    correlation_id: str = Field(min_length=1, max_length=36)
    consensus_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    experiment_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    strategy: str = Field(min_length=1, max_length=128)
    action: CandidateAction
    confidence: int = Field(ge=0, le=100)
    balance: float = Field(gt=0)
    requested_weight_percent: float = Field(ge=0, le=100)
    capped_weight_percent: float = Field(ge=0, le=100)
    max_notional: float = Field(ge=0)
    current_gross_exposure: float = Field(ge=0)
    current_net_exposure: float
    max_target_weight_percent: float = Field(gt=0, le=100)
    max_gross_exposure_percent: float = Field(gt=0)
    max_net_exposure_percent: float = Field(gt=0)
    max_symbol_exposure_percent: float = Field(gt=0)
    max_strategy_exposure_percent: float = Field(gt=0)
    max_single_position_percent: float = Field(gt=0)
    status: PortfolioProposalStatus
    binding_limits: list[str] = Field(default_factory=list)
    decision_authority: Literal[False] = False
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_and_assign_identity(self) -> "PortfolioProposal":
        if self.capped_weight_percent > self.requested_weight_percent + 1e-9:
            raise ValueError("portfolio construction cannot increase target weight")
        expected_notional = self.balance * self.capped_weight_percent / 100
        if not math.isclose(
            self.max_notional,
            expected_notional,
            rel_tol=1e-8,
            abs_tol=1e-6,
        ):
            raise ValueError("max_notional must match the capped target weight")
        directional = self.action in {
            CandidateAction.BUY,
            CandidateAction.SELL,
        }
        if self.status == "PROPOSED" and (
            not directional or self.max_notional <= 0
        ):
            raise ValueError("proposed portfolio target must be executable")
        if self.status != "PROPOSED" and self.max_notional != 0:
            raise ValueError("non-proposed portfolio artifact must have zero notional")
        identity = _sha256(
            {
                key: value
                for key, value in self.model_dump(mode="json").items()
                if key != "proposal_id"
            }
        )
        if self.proposal_id and self.proposal_id != identity:
            raise ValueError("proposal_id does not match immutable proposal")
        self.proposal_id = identity
        return self

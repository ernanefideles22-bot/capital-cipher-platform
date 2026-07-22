"""Month 9 portfolio construction, experiments, consensus and drift."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.agents.evaluation import AgentEvaluationService
from app.schemas.agents import AgentOutput, AgentRegistration
from app.schemas.common import (
    AgentStatus,
    CandidateAction,
    OrderSide,
    Signal,
)
from app.schemas.decisions import Decision
from app.schemas.portfolio_consensus import (
    ConsensusAgentWeight,
    ConsensusExperiment,
    ConsensusExperimentEvent,
    DriftObservation,
    PortfolioProposal,
    WeightedConsensus,
)
from app.schemas.risk import PositionExposure, RiskLimits


class ConsensusExperimentService:
    """Append-only experiment registry with event-sourced activation."""

    def __init__(self, repository=None) -> None:
        self._repository = repository
        default = ConsensusExperiment(
            name="month9_weighted_consensus",
            version="1.0.0",
            mode="SHADOW",
            created_by="platform",
        )
        created = ConsensusExperimentEvent(
            experiment_id=default.experiment_id,
            event_type="CREATED",
            actor="platform",
            reason="Safe Month 9 default; observational until explicitly activated",
            created_at=default.created_at,
        )
        self._default_id = default.experiment_id
        self._experiments = {default.experiment_id: default}
        self._events = {created.event_id: created}

    async def initialize(self) -> None:
        if self._repository is None:
            return
        experiments = await self._repository.list_consensus_experiments(
            limit=10_000
        )
        events = await self._repository.list_consensus_experiment_events(
            limit=100_000
        )
        default = self._experiments[self._default_id]
        default_event = next(iter(self._events.values()))
        if default.experiment_id not in {
            item.experiment_id for item in experiments
        }:
            await self._repository.save_consensus_experiment_definition(
                default,
                default_event,
            )
            experiments.append(default)
            events.append(default_event)
        self._experiments = {
            item.experiment_id: item for item in experiments
        }
        self._events = {item.event_id: item for item in events}

    async def register(
        self,
        experiment: ConsensusExperiment,
    ) -> ConsensusExperiment:
        existing = self._experiments.get(experiment.experiment_id)
        if existing is not None:
            if existing != experiment:
                raise ValueError("Immutable experiment identity conflict")
            return existing
        if any(
            item.name == experiment.name and item.version == experiment.version
            for item in self._experiments.values()
        ):
            raise ValueError("Experiment name and version already exist")
        if abs(
            (datetime.now(timezone.utc) - experiment.created_at).total_seconds()
        ) > 300:
            raise ValueError(
                "Experiment created_at must be within the server time window"
            )
        event = ConsensusExperimentEvent(
            experiment_id=experiment.experiment_id,
            event_type="CREATED",
            actor=experiment.created_by,
            reason="Versioned experiment definition registered",
            created_at=experiment.created_at,
        )
        if self._repository is not None:
            await self._repository.save_consensus_experiment_definition(
                experiment,
                event,
            )
        self._experiments[experiment.experiment_id] = experiment
        self._events[event.event_id] = event
        return experiment

    async def record_event(
        self,
        event: ConsensusExperimentEvent,
    ) -> ConsensusExperimentEvent:
        if event.experiment_id not in self._experiments:
            raise ValueError("Experiment does not exist")
        if event.event_type == "CREATED":
            raise ValueError("CREATED is emitted only during registration")
        existing = self._events.get(event.event_id)
        if existing is not None:
            if existing != event:
                raise ValueError("Immutable experiment event conflict")
            return existing
        now = datetime.now(timezone.utc)
        if abs((now - event.created_at).total_seconds()) > 300:
            raise ValueError(
                "Experiment event created_at must be within the server time window"
            )
        latest_time = max(item.created_at for item in self._events.values())
        if event.created_at < latest_time:
            raise ValueError("Experiment lifecycle events must be monotonic")
        activated_id = self._activated_experiment_id()
        if (
            event.event_type == "ACTIVATED"
            and activated_id == event.experiment_id
        ):
            raise ValueError("Experiment is already active")
        if (
            event.event_type == "RETIRED"
            and activated_id != event.experiment_id
        ):
            raise ValueError("Only the active experiment may be retired")
        if self._repository is not None:
            await self._repository.save_consensus_experiment_event(event)
        self._events[event.event_id] = event
        return event

    def _activated_experiment_id(self) -> str | None:
        active_id: str | None = None
        for event in sorted(
            self._events.values(),
            key=lambda item: (item.created_at, item.event_id),
        ):
            if event.event_type == "ACTIVATED":
                active_id = event.experiment_id
            elif (
                event.event_type == "RETIRED"
                and event.experiment_id == active_id
            ):
                active_id = None
        return active_id

    def active(self) -> ConsensusExperiment:
        active_id = self._activated_experiment_id()
        return self._experiments.get(
            active_id or self._default_id,
            self._experiments[self._default_id],
        )

    def list_experiments(self) -> list[ConsensusExperiment]:
        return sorted(
            self._experiments.values(),
            key=lambda item: (item.created_at, item.experiment_id),
            reverse=True,
        )

    def list_events(self) -> list[ConsensusExperimentEvent]:
        return sorted(
            self._events.values(),
            key=lambda item: (item.created_at, item.event_id),
            reverse=True,
        )


class DriftMonitor:
    """Detect rolling degradation and exclude only the affected agent version."""

    def __init__(
        self,
        evaluation: AgentEvaluationService,
        repository=None,
    ) -> None:
        self._evaluation = evaluation
        self._repository = repository
        self._observations: dict[str, DriftObservation] = {}

    async def initialize(self) -> None:
        if self._repository is not None:
            items = await self._repository.list_drift_observations(
                limit=100_000
            )
            self._observations = {
                item.observation_id: item for item in items
            }

    async def observe(
        self,
        experiment: ConsensusExperiment,
        registrations: dict[str, AgentRegistration],
    ) -> dict[tuple[str, str], DriftObservation]:
        results: dict[tuple[str, str], DriftObservation] = {}
        pending: list[DriftObservation] = []
        for registration in registrations.values():
            history = await self._evaluation.settled_history(
                agent_name=registration.agent_name,
                agent_version=registration.version,
            )
            required = (
                experiment.drift_reference_samples
                + experiment.drift_current_samples
            )
            if len(history) < required:
                continue
            reference_pairs = history[
                -required : -experiment.drift_current_samples
            ]
            current_pairs = history[-experiment.drift_current_samples :]
            reference = [item[1] for item in reference_pairs]
            current = [item[1] for item in current_pairs]
            reference_directional = [
                item for item in reference if item.correct is not None
            ]
            current_directional = [
                item for item in current if item.correct is not None
            ]
            reference_accuracy = (
                sum(item.correct is True for item in reference_directional)
                / len(reference_directional)
                if reference_directional and current_directional
                else None
            )
            current_accuracy = (
                sum(item.correct is True for item in current_directional)
                / len(current_directional)
                if reference_directional and current_directional
                else None
            )
            accuracy_delta = (
                current_accuracy - reference_accuracy
                if reference_accuracy is not None
                and current_accuracy is not None
                else None
            )
            reference_brier = statistics.fmean(
                item.brier_loss for item in reference
            )
            current_brier = statistics.fmean(
                item.brier_loss for item in current
            )
            reference_marginal = statistics.fmean(
                item.marginal_contribution for item in reference
            )
            current_marginal = statistics.fmean(
                item.marginal_contribution for item in current
            )
            brier_delta = current_brier - reference_brier
            marginal_delta = current_marginal - reference_marginal
            critical: list[str] = []
            warning: list[str] = []
            if (
                accuracy_delta is not None
                and accuracy_delta <= -experiment.critical_accuracy_drop
            ):
                critical.append("ACCURACY_DROP")
            elif (
                accuracy_delta is not None
                and accuracy_delta
                <= -(experiment.critical_accuracy_drop / 2)
            ):
                warning.append("ACCURACY_DROP")
            if brier_delta >= experiment.critical_brier_increase:
                critical.append("BRIER_LOSS_INCREASE")
            elif brier_delta >= experiment.critical_brier_increase / 2:
                warning.append("BRIER_LOSS_INCREASE")
            if marginal_delta <= -experiment.critical_marginal_drop:
                critical.append("MARGINAL_CONTRIBUTION_DROP")
            elif marginal_delta <= -(experiment.critical_marginal_drop / 2):
                warning.append("MARGINAL_CONTRIBUTION_DROP")
            severity = (
                "CRITICAL"
                if critical
                else "WARNING"
                if warning
                else "NONE"
            )
            observation = DriftObservation(
                experiment_id=experiment.experiment_id,
                agent_name=registration.agent_name,
                agent_version=registration.version,
                reference_samples=len(reference),
                current_samples=len(current),
                reference_accuracy=reference_accuracy,
                current_accuracy=current_accuracy,
                accuracy_delta=accuracy_delta,
                reference_brier_loss=reference_brier,
                current_brier_loss=current_brier,
                brier_delta=brier_delta,
                reference_marginal_contribution=reference_marginal,
                current_marginal_contribution=current_marginal,
                marginal_delta=marginal_delta,
                severity=severity,
                reasons=sorted(set([*critical, *warning])),
                observed_at=current[-1].realized_at,
            )
            pending.append(observation)
        if self._repository is not None:
            pending = await self._repository.save_drift_observations(pending)
        for observation in pending:
            self._observations[observation.observation_id] = observation
            results[
                (observation.agent_name, observation.agent_version)
            ] = observation
        return results

    def list(self, *, limit: int = 100) -> list[DriftObservation]:
        return sorted(
            self._observations.values(),
            key=lambda item: (item.observed_at, item.observation_id),
            reverse=True,
        )[:limit]


def _capped_weights(
    scores: list[float],
    cap: float,
) -> list[float]:
    """Normalize positive scores with deterministic concentration capping."""

    remaining = set(range(len(scores)))
    weights = [0.0] * len(scores)
    remaining_mass = 1.0
    while remaining:
        total = sum(scores[index] for index in remaining)
        provisional = {
            index: remaining_mass * scores[index] / total
            for index in remaining
        }
        capped = {
            index for index, value in provisional.items() if value > cap
        }
        if not capped:
            for index, value in provisional.items():
                weights[index] = value
            break
        for index in capped:
            weights[index] = cap
            remaining_mass -= cap
            remaining.remove(index)
    rounded = [round(value, 12) for value in weights]
    rounded[-1] = round(rounded[-1] + (1 - sum(rounded)), 12)
    return rounded


class WeightedConsensusService:
    """Performance weighting that can only confirm or veto primary direction."""

    def __init__(
        self,
        evaluation: AgentEvaluationService,
        experiments: ConsensusExperimentService,
        drift_monitor: DriftMonitor,
        repository=None,
    ) -> None:
        self._evaluation = evaluation
        self._experiments = experiments
        self._drift = drift_monitor
        self._repository = repository
        self._snapshots: dict[str, WeightedConsensus] = {}

    async def initialize(self) -> None:
        if self._repository is not None:
            items = await self._repository.list_weighted_consensus(
                limit=100_000
            )
            self._snapshots = {
                item.consensus_id: item for item in items
            }

    async def evaluate(
        self,
        *,
        baseline: Decision,
        outputs: list[AgentOutput],
        registrations: dict[str, AgentRegistration],
    ) -> WeightedConsensus:
        experiment = self._experiments.active()
        scorecards = {
            (item.agent_name, item.agent_version): item
            for item in await self._evaluation.scorecards()
        }
        drift = await self._drift.observe(experiment, registrations)
        excluded: dict[str, str] = {}
        candidates: list[tuple[AgentOutput, AgentRegistration, Any, float]] = []
        for output in outputs:
            registration = registrations.get(output.agent_name)
            if registration is None or registration.decision_role != "SHADOW":
                continue
            card = scorecards.get(
                (registration.agent_name, registration.version)
            )
            reason = None
            if output.status != AgentStatus.COMPLETED:
                reason = f"OUTPUT_{output.status.value}"
            elif card is None or card.sample_count < experiment.minimum_samples:
                reason = "INSUFFICIENT_SAMPLE"
            elif (
                card.directional_sample_count
                < experiment.minimum_directional_samples
            ):
                reason = "INSUFFICIENT_DIRECTIONAL_SAMPLE"
            elif card.accuracy is None or (
                card.accuracy < experiment.minimum_accuracy
            ):
                reason = "ACCURACY_BELOW_THRESHOLD"
            elif card.mean_brier_loss is None or (
                card.mean_brier_loss > experiment.maximum_brier_loss
            ):
                reason = "BRIER_LOSS_ABOVE_THRESHOLD"
            elif card.mean_marginal_contribution is None or (
                card.mean_marginal_contribution
                <= experiment.minimum_marginal_contribution
            ):
                reason = "NON_POSITIVE_MARGINAL_CONTRIBUTION"
            elif (
                current_drift := drift.get(
                    (registration.agent_name, registration.version)
                )
            ) is not None and current_drift.severity == "CRITICAL":
                reason = "CRITICAL_DRIFT"
            if reason is not None:
                excluded[output.agent_name] = reason
                continue
            assert card is not None
            assert card.accuracy is not None
            assert card.mean_brier_loss is not None
            assert card.mean_marginal_contribution is not None
            raw_score = max(
                1e-9,
                card.accuracy
                * (1 - card.mean_brier_loss)
                * (1 + card.mean_marginal_contribution),
            )
            candidates.append(
                (output, registration, card, raw_score)
            )

        if len(candidates) < experiment.minimum_eligible_agents:
            artifact = WeightedConsensus(
                correlation_id=baseline.correlation_id,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.version,
                mode=experiment.mode,
                symbol=baseline.symbol,
                timeframe=baseline.timeframe,
                status="INSUFFICIENT_DATA",
                baseline_action=baseline.candidate_action,
                baseline_confidence=baseline.confidence,
                eligible_agent_count=0,
                excluded_agents=excluded,
                final_action=baseline.candidate_action,
                final_confidence=baseline.confidence,
                reason=(
                    "Performance consensus unavailable; static primary "
                    "decision preserved"
                ),
            )
        else:
            normalized = _capped_weights(
                [item[3] for item in candidates],
                experiment.maximum_agent_weight,
            )
            weights = []
            probability_up = 0.0
            for (output, registration, card, raw_score), weight in zip(
                candidates,
                normalized,
            ):
                probability = AgentEvaluationService.probability(output)
                probability_up += probability * weight
                weights.append(
                    ConsensusAgentWeight(
                        agent_name=registration.agent_name,
                        agent_version=registration.version,
                        sample_count=card.sample_count,
                        directional_sample_count=(
                            card.directional_sample_count
                        ),
                        accuracy=card.accuracy,
                        mean_brier_loss=card.mean_brier_loss,
                        mean_marginal_contribution=(
                            card.mean_marginal_contribution
                        ),
                        probability_up=probability,
                        raw_score=raw_score,
                        weight=weight,
                    )
                )
            if probability_up >= experiment.buy_probability_threshold:
                signal = Signal.BUY
            elif probability_up <= experiment.sell_probability_threshold:
                signal = Signal.SELL
            else:
                signal = Signal.HOLD
            consensus_confidence = min(
                100,
                int(round(abs(probability_up - 0.5) * 200)),
            )
            final_action = baseline.candidate_action
            final_confidence = baseline.confidence
            applied = False
            reason = "Weighted consensus recorded in shadow mode"
            if (
                experiment.mode == "CONFIRMATION"
                and baseline.candidate_action
                in {CandidateAction.BUY, CandidateAction.SELL}
            ):
                applied = True
                agrees = signal.value == baseline.candidate_action.value
                if agrees:
                    reason = (
                        "Eligible performance-weighted agents confirmed "
                        "the primary direction"
                    )
                else:
                    final_action = CandidateAction.WAIT
                    final_confidence = 0
                    reason = (
                        "Consensus did not confirm the primary direction; "
                        "candidate tightened to WAIT"
                    )
            artifact = WeightedConsensus(
                correlation_id=baseline.correlation_id,
                experiment_id=experiment.experiment_id,
                experiment_version=experiment.version,
                mode=experiment.mode,
                symbol=baseline.symbol,
                timeframe=baseline.timeframe,
                status="READY",
                baseline_action=baseline.candidate_action,
                baseline_confidence=baseline.confidence,
                probability_up=round(probability_up, 12),
                signal=signal,
                consensus_confidence=consensus_confidence,
                eligible_agent_count=len(weights),
                excluded_agents=excluded,
                weights=weights,
                applied=applied,
                final_action=final_action,
                final_confidence=final_confidence,
                reason=reason,
            )
        if self._repository is not None:
            artifact = await self._repository.save_weighted_consensus(
                artifact
            )
        self._snapshots[artifact.consensus_id] = artifact
        return artifact

    def list(self, *, limit: int = 100) -> list[WeightedConsensus]:
        return sorted(
            self._snapshots.values(),
            key=lambda item: (item.created_at, item.consensus_id),
            reverse=True,
        )[:limit]


class PortfolioConstructionService:
    """Construct bounded advisory targets before central risk evaluation."""

    def __init__(
        self,
        limits: RiskLimits,
        risk_manager,
        repository=None,
        *,
        max_target_weight_percent: float = 25.0,
    ) -> None:
        if not 0 < max_target_weight_percent <= 100:
            raise ValueError("Portfolio target weight must be in (0, 100]")
        self._limits = limits
        self._risk = risk_manager
        self._repository = repository
        self._max_target_weight = max_target_weight_percent
        self._proposals: dict[str, PortfolioProposal] = {}

    async def initialize(self) -> None:
        if self._repository is not None:
            items = await self._repository.list_portfolio_proposals(
                limit=100_000
            )
            self._proposals = {
                item.proposal_id: item for item in items
            }

    async def propose(
        self,
        *,
        decision: Decision,
        consensus: WeightedConsensus,
        balance: float,
    ) -> PortfolioProposal:
        positions: list[PositionExposure] = (
            self._risk.position_exposures()
        )
        gross = sum(item.notional for item in positions)
        net = sum(
            item.notional if item.side == OrderSide.BUY else -item.notional
            for item in positions
        )
        requested = (
            self._max_target_weight * decision.confidence / 100
            if decision.candidate_action
            in {CandidateAction.BUY, CandidateAction.SELL}
            else 0.0
        )
        status = "NO_ACTION"
        capped = 0.0
        binding: list[str] = []
        if requested > 0:
            symbol = sum(
                item.notional
                for item in positions
                if item.symbol == decision.symbol
            )
            strategy = sum(
                item.notional
                for item in positions
                if item.strategy == decision.strategy
            )
            net_percent = net / balance * 100
            capacities = {
                "PORTFOLIO_TARGET_LIMIT": self._max_target_weight,
                "GROSS_EXPOSURE_LIMIT": (
                    self._limits.max_gross_exposure_percent
                    - gross / balance * 100
                ),
                "NET_EXPOSURE_LIMIT": (
                    self._limits.max_net_exposure_percent - net_percent
                    if decision.candidate_action == CandidateAction.BUY
                    else self._limits.max_net_exposure_percent + net_percent
                ),
                "SYMBOL_EXPOSURE_LIMIT": (
                    self._limits.max_symbol_exposure_percent
                    - symbol / balance * 100
                ),
                "STRATEGY_EXPOSURE_LIMIT": (
                    self._limits.max_strategy_exposure_percent
                    - strategy / balance * 100
                ),
                "SINGLE_POSITION_LIMIT": (
                    self._limits.max_single_position_percent
                ),
            }
            capped = max(0.0, min(requested, *capacities.values()))
            binding = sorted(
                name
                for name, capacity in capacities.items()
                if capacity <= capped + 1e-9
            )
            status = "PROPOSED" if capped > 0 else "BLOCKED"
        proposal = PortfolioProposal(
            correlation_id=decision.correlation_id,
            consensus_id=consensus.consensus_id,
            experiment_id=consensus.experiment_id,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            strategy=decision.strategy,
            action=decision.candidate_action,
            confidence=decision.confidence,
            balance=balance,
            requested_weight_percent=round(requested, 12),
            capped_weight_percent=round(capped, 12),
            max_notional=round(balance * capped / 100, 8),
            current_gross_exposure=round(gross, 8),
            current_net_exposure=round(net, 8),
            max_target_weight_percent=self._max_target_weight,
            max_gross_exposure_percent=(
                self._limits.max_gross_exposure_percent
            ),
            max_net_exposure_percent=self._limits.max_net_exposure_percent,
            max_symbol_exposure_percent=(
                self._limits.max_symbol_exposure_percent
            ),
            max_strategy_exposure_percent=(
                self._limits.max_strategy_exposure_percent
            ),
            max_single_position_percent=(
                self._limits.max_single_position_percent
            ),
            status=status,
            binding_limits=binding,
        )
        if self._repository is not None:
            proposal = await self._repository.save_portfolio_proposal(
                proposal
            )
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def list(self, *, limit: int = 100) -> list[PortfolioProposal]:
        return sorted(
            self._proposals.values(),
            key=lambda item: (item.created_at, item.proposal_id),
            reverse=True,
        )[:limit]

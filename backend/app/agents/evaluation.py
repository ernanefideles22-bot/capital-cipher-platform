"""Governed evidence store and observational PAPER-agent evaluation."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.schemas.agents import AgentOutput, AgentRegistration
from app.schemas.common import Signal
from app.schemas.market import Candle
from app.schemas.specialist_evaluation import (
    AgentForecast,
    AgentForecastOutcome,
    AgentScorecard,
    SpecialistEvidence,
    SpecialistDomain,
)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def timeframe_seconds(timeframe: str) -> int:
    amount = int(timeframe[:-1])
    unit = timeframe[-1]
    return amount * {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800}[unit]


class SpecialistEvidenceService:
    def __init__(self, repository=None) -> None:
        self._repository = repository
        self._items: dict[str, SpecialistEvidence] = {}
        self._source_events: dict[tuple[str, str], str] = {}

    async def initialize(self) -> None:
        if self._repository is not None:
            for evidence in await self._repository.list_specialist_evidence(
                limit=10_000
            ):
                self._items[evidence.evidence_id] = evidence
                self._source_events[
                    (evidence.source, evidence.source_event_id)
                ] = evidence.evidence_id

    async def ingest(self, evidence: SpecialistEvidence) -> SpecialistEvidence:
        source_key = (evidence.source, evidence.source_event_id)
        source_identity = self._source_events.get(source_key)
        if (
            source_identity is not None
            and source_identity != evidence.evidence_id
        ):
            raise ValueError(
                "Source event already maps to different immutable evidence"
            )
        existing = self._items.get(evidence.evidence_id)
        if existing is not None:
            if existing != evidence:
                raise ValueError("Immutable evidence identity conflict")
            return existing
        if self._repository is not None:
            evidence = await self._repository.save_specialist_evidence(evidence)
        self._items[evidence.evidence_id] = evidence
        self._source_events[source_key] = evidence.evidence_id
        return evidence

    async def latest(
        self,
        *,
        domain: SpecialistDomain,
        metric_name: str,
        scope: str,
        as_of: datetime,
    ) -> SpecialistEvidence | None:
        eligible = [
            item
            for item in self._items.values()
            if item.domain == domain
            and item.metric_name == metric_name
            and item.scope == scope
            and item.observed_at <= as_of
        ]
        return max(eligible, key=lambda item: item.observed_at) if eligible else None

    async def list(
        self,
        *,
        domain: SpecialistDomain | None = None,
        metric_name: str | None = None,
        scope: str | None = None,
        limit: int = 100,
    ) -> list[SpecialistEvidence]:
        items = [
            item
            for item in self._items.values()
            if (domain is None or item.domain == domain)
            and (metric_name is None or item.metric_name == metric_name)
            and (scope is None or item.scope == scope)
        ]
        return sorted(
            items,
            key=lambda item: (item.observed_at, item.evidence_id),
            reverse=True,
        )[:limit]


class AgentEvaluationService:
    """Settles one-candle forecasts without influencing execution authority."""

    def __init__(self, repository=None, *, minimum_samples: int = 30) -> None:
        self._repository = repository
        self._minimum_samples = minimum_samples
        self._forecasts: dict[str, AgentForecast] = {}
        self._outcomes: dict[str, AgentForecastOutcome] = {}

    async def initialize(self) -> None:
        if self._repository is not None:
            forecasts = await self._repository.list_agent_forecasts(limit=100_000)
            outcomes = await self._repository.list_agent_forecast_outcomes(
                limit=100_000
            )
            self._forecasts = {item.forecast_id: item for item in forecasts}
            self._outcomes = {item.forecast_id: item for item in outcomes}

    @staticmethod
    def probability(output: AgentOutput) -> float:
        conviction = output.confidence / 200
        if output.signal == Signal.BUY:
            return 0.5 + conviction
        if output.signal == Signal.SELL:
            return 0.5 - conviction
        return 0.5

    async def observe(
        self,
        *,
        candle: Candle,
        correlation_id: str,
        outputs: list[AgentOutput],
        registrations: dict[str, AgentRegistration],
    ) -> tuple[list[AgentForecastOutcome], list[AgentForecast]]:
        outcomes = await self.settle(candle)
        horizon = timeframe_seconds(candle.timeframe)
        forecasts = [
            AgentForecast(
                correlation_id=correlation_id,
                agent_name=output.agent_name,
                agent_version=registrations[output.agent_name].version,
                definition_hash=registrations[output.agent_name].definition_hash,
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                signal=output.signal,
                confidence=output.confidence,
                probability_up=self.probability(output),
                reference_price=candle.close,
                forecast_at=candle.closed_at,
                target_at=candle.closed_at + timedelta(seconds=horizon),
                horizon_seconds=horizon,
                decision_role=registrations[output.agent_name].decision_role,
                created_at=candle.received_at,
            )
            for output in outputs
        ]
        if self._repository is not None:
            forecasts = await self._repository.save_agent_forecasts(forecasts)
        self._forecasts.update({item.forecast_id: item for item in forecasts})
        return outcomes, forecasts

    async def settle(self, candle: Candle) -> list[AgentForecastOutcome]:
        cohort = [
            item
            for item in self._forecasts.values()
            if item.forecast_id not in self._outcomes
            and item.symbol == candle.symbol
            and item.timeframe == candle.timeframe
            and item.target_at == candle.closed_at
        ]
        if not cohort:
            return []
        grouped: dict[datetime, list[AgentForecast]] = defaultdict(list)
        for item in cohort:
            grouped[item.target_at].append(item)
        outcomes: list[AgentForecastOutcome] = []
        for target_at, group in sorted(grouped.items()):
            probabilities = [item.probability_up for item in group]
            ensemble = statistics.fmean(probabilities)
            realized_return = candle.close / group[0].reference_price - 1
            realized_up = 1.0 if realized_return > 0 else 0.0 if realized_return < 0 else 0.5
            ensemble_loss = (ensemble - realized_up) ** 2
            for forecast in group:
                other = [
                    item.probability_up
                    for item in group
                    if item.forecast_id != forecast.forecast_id
                ]
                leave_one_out = statistics.fmean(other) if other else 0.5
                leave_loss = (leave_one_out - realized_up) ** 2
                correct = None
                if (
                    forecast.probability_up != 0.5
                    and realized_up != 0.5
                ):
                    correct = (
                        forecast.probability_up > 0.5
                        if realized_up == 1
                        else forecast.probability_up < 0.5
                    )
                outcomes.append(
                    AgentForecastOutcome(
                        forecast_id=forecast.forecast_id,
                        realized_at=candle.closed_at,
                        realized_price=candle.close,
                        realized_return=realized_return,
                        realized_up=realized_up,
                        correct=correct,
                        brier_loss=(forecast.probability_up - realized_up) ** 2,
                        ensemble_probability_up=ensemble,
                        ensemble_brier_loss=ensemble_loss,
                        leave_one_out_probability_up=leave_one_out,
                        leave_one_out_brier_loss=leave_loss,
                        marginal_contribution=leave_loss - ensemble_loss,
                        cohort_size=len(group),
                        created_at=candle.received_at,
                    )
                )
        if self._repository is not None:
            outcomes = await self._repository.save_agent_forecast_outcomes(outcomes)
        self._outcomes.update({item.forecast_id: item for item in outcomes})
        return outcomes

    async def scorecards(self) -> list[AgentScorecard]:
        forecasts = self._forecasts
        grouped: dict[tuple[str, str], list[AgentForecastOutcome]] = defaultdict(list)
        for forecast_id, outcome in self._outcomes.items():
            forecast = forecasts.get(forecast_id)
            if forecast is not None:
                grouped[(forecast.agent_name, forecast.agent_version)].append(outcome)
        cards = []
        for (name, version), outcomes in grouped.items():
            directional = [item for item in outcomes if item.correct is not None]
            count = len(outcomes)
            cards.append(
                AgentScorecard(
                    agent_name=name,
                    agent_version=version,
                    sample_count=count,
                    directional_sample_count=len(directional),
                    accuracy=(
                        sum(item.correct is True for item in directional) / len(directional)
                        if directional
                        else None
                    ),
                    mean_brier_loss=statistics.fmean(item.brier_loss for item in outcomes),
                    mean_marginal_contribution=statistics.fmean(
                        item.marginal_contribution for item in outcomes
                    ),
                    status=(
                        "EVALUATED"
                        if count >= self._minimum_samples
                        else "INSUFFICIENT_SAMPLE"
                    ),
                    minimum_samples=self._minimum_samples,
                )
            )
        return sorted(cards, key=lambda item: item.agent_name)

    async def forecasts(self, *, limit: int = 100) -> list[AgentForecast]:
        return sorted(
            self._forecasts.values(),
            key=lambda item: (item.forecast_at, item.forecast_id),
            reverse=True,
        )[:limit]

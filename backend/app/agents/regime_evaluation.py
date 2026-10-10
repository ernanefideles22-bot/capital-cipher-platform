"""Read-only specialist performance attribution by historical market regime."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from app.agents.regime import classify_market_regime
from app.market_data.store import CandleStore
from app.schemas.common import Exchange, MarketRegime

MINIMUM_REGIME_SAMPLES = 30
RANDOM_BRIER_BASELINE = 0.25


class RegimeShadowEvaluationService:
    """Attribute settled agent forecasts to the regime known at forecast time.

    The service has no decision, risk, weight, or order authority. Regime is
    recomputed with the same deterministic classifier used by TrendAgent, using
    only candles whose closed_at is <= forecast.forecast_at.
    """

    def __init__(self, evaluation_service, candle_store: CandleStore) -> None:
        self._evaluation = evaluation_service
        self._store = candle_store

    def _candles_at_forecast(self, forecast) -> list:
        candidates: list[list] = []
        for exchange in Exchange:
            candles = self._store.get(
                exchange.value,
                forecast.symbol,
                forecast.timeframe,
                limit=500,
            )
            historical = [
                candle
                for candle in candles
                if candle.closed_at <= forecast.forecast_at
            ]
            if historical:
                candidates.append(historical)
        if not candidates:
            return []
        return max(candidates, key=lambda items: items[-1].closed_at)

    @staticmethod
    def _qualify_row(row: dict[str, Any]) -> None:
        accuracy = row["accuracy"]
        criteria = {
            "minimum_sample_reached": row["sample_count"] >= MINIMUM_REGIME_SAMPLES,
            "accuracy_above_50_percent": accuracy is not None and accuracy > 0.50,
            "brier_below_random_baseline": row["mean_brier_loss"] < RANDOM_BRIER_BASELINE,
            "positive_marginal_contribution": row["mean_marginal_contribution"] > 0,
        }
        qualified = all(criteria.values())
        row["criteria"] = criteria
        row["qualified_shadow_specialist"] = qualified
        row["progress_percent"] = min(
            100.0,
            row["sample_count"] / MINIMUM_REGIME_SAMPLES * 100.0,
        )
        if row["sample_count"] < MINIMUM_REGIME_SAMPLES:
            row["status"] = "COLLECTING"
        elif qualified:
            row["status"] = "SHADOW_SPECIALIST"
        else:
            row["status"] = "OBSERVED_NOT_QUALIFIED"

    async def report(self, *, candidate_names: set[str] | None = None) -> dict[str, Any]:
        scorecards = await self._evaluation.scorecards()
        rows: dict[tuple[str, str, str], list] = defaultdict(list)
        unavailable_history = 0

        for card in scorecards:
            if candidate_names is not None and card.agent_name not in candidate_names:
                continue
            history = await self._evaluation.settled_history(
                agent_name=card.agent_name,
                agent_version=card.agent_version,
            )
            for forecast, outcome in history:
                candles = self._candles_at_forecast(forecast)
                if not candles:
                    unavailable_history += 1
                    continue
                regime = classify_market_regime(candles).regime
                rows[(card.agent_name, card.agent_version, regime.value)].append(
                    outcome
                )

        report: list[dict[str, Any]] = []
        for (agent_name, agent_version, regime), outcomes in rows.items():
            directional = [item for item in outcomes if item.correct is not None]
            sample_count = len(outcomes)
            directional_count = len(directional)
            accuracy = (
                sum(item.correct is True for item in directional) / directional_count
                if directional_count
                else None
            )
            row = {
                "agent_name": agent_name,
                "agent_version": agent_version,
                "market_regime": regime,
                "sample_count": sample_count,
                "directional_sample_count": directional_count,
                "accuracy": accuracy,
                "mean_brier_loss": statistics.fmean(
                    item.brier_loss for item in outcomes
                ),
                "mean_marginal_contribution": statistics.fmean(
                    item.marginal_contribution for item in outcomes
                ),
                "sample_sufficient": sample_count >= MINIMUM_REGIME_SAMPLES,
                "minimum_samples": MINIMUM_REGIME_SAMPLES,
                "decision_authority": False,
                "automatic_weight_adjustment": False,
            }
            self._qualify_row(row)
            report.append(row)

        regime_order = {regime.value: index for index, regime in enumerate(MarketRegime)}
        report.sort(
            key=lambda row: (
                row["agent_name"],
                regime_order.get(row["market_regime"], 999),
            )
        )

        by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in report:
            by_regime[row["market_regime"]].append(row)
        for regime_rows in by_regime.values():
            regime_rows.sort(
                key=lambda row: (
                    not row["qualified_shadow_specialist"],
                    not row["sample_sufficient"],
                    -(row["accuracy"] if row["accuracy"] is not None else -1.0),
                    row["mean_brier_loss"],
                    -row["mean_marginal_contribution"],
                    row["agent_name"],
                )
            )
            for rank, row in enumerate(regime_rows, start=1):
                row["rank_within_regime"] = rank

        return {
            "rows": report,
            "unavailable_historical_forecasts": unavailable_history,
            "shadow_specialist_count": sum(
                row["qualified_shadow_specialist"] for row in report
            ),
            "collecting_count": sum(row["status"] == "COLLECTING" for row in report),
            "observed_not_qualified_count": sum(
                row["status"] == "OBSERVED_NOT_QUALIFIED" for row in report
            ),
            "decision_authority": False,
            "automatic_weight_adjustment": False,
            "lookahead_protection": "candles.closed_at <= forecast.forecast_at",
            "classifier": "TrendAgent shared deterministic classifier",
            "qualification": {
                "minimum_samples": MINIMUM_REGIME_SAMPLES,
                "accuracy_above": 0.50,
                "brier_below": RANDOM_BRIER_BASELINE,
                "marginal_contribution_above": 0.0,
            },
        }

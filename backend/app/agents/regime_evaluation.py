"""Read-only specialist performance attribution by historical market regime."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any

from app.agents.regime import classify_market_regime
from app.market_data.store import CandleStore
from app.schemas.common import Exchange, MarketRegime


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
            report.append(
                {
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
                    "sample_sufficient": sample_count >= 30,
                    "minimum_samples": 30,
                    "decision_authority": False,
                    "automatic_weight_adjustment": False,
                }
            )

        regime_order = {regime.value: index for index, regime in enumerate(MarketRegime)}
        report.sort(
            key=lambda row: (
                row["agent_name"],
                regime_order.get(row["market_regime"], 999),
            )
        )
        return {
            "rows": report,
            "unavailable_historical_forecasts": unavailable_history,
            "decision_authority": False,
            "automatic_weight_adjustment": False,
            "lookahead_protection": "candles.closed_at <= forecast.forecast_at",
            "classifier": "TrendAgent shared deterministic classifier",
        }

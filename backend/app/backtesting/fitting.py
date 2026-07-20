"""Train-only fitting boundary for pre-registered walk-forward candidates."""

from __future__ import annotations

import statistics
from typing import Protocol

from app.backtesting.artifacts import canonical_sha256
from app.schemas.backtest import (
    WalkForwardFittedCandidate,
    WalkForwardSegment,
)
from app.schemas.market import Candle

FROZEN_FITTER_VERSION = "frozen-strategy-fitter-v1"


class WalkForwardCandidateFitter(Protocol):
    version: str

    def fit(
        self,
        *,
        candidate_version: str,
        fold_index: int,
        train_segment: WalkForwardSegment,
        train_candles: list[Candle],
        strategy_parameters: dict,
    ) -> WalkForwardFittedCandidate: ...


class FrozenStrategyFitter:
    """Consumes train data but cannot mutate the pre-registered strategy."""

    version = FROZEN_FITTER_VERSION

    def fit(
        self,
        *,
        candidate_version: str,
        fold_index: int,
        train_segment: WalkForwardSegment,
        train_candles: list[Candle],
        strategy_parameters: dict,
    ) -> WalkForwardFittedCandidate:
        if len(train_candles) != train_segment.candles:
            raise ValueError(
                "Train-only fitter received an unexpected candle count"
            )
        returns = [
            (current.close / previous.close) - 1
            for previous, current in zip(
                train_candles,
                train_candles[1:],
            )
            if previous.close > 0
        ]
        diagnostics = {
            "mean_return": (
                float(statistics.fmean(returns)) if returns else 0.0
            ),
            "return_volatility": (
                float(statistics.pstdev(returns))
                if len(returns) > 1
                else 0.0
            ),
            "median_volume": float(
                statistics.median(
                    candle.volume for candle in train_candles
                )
            ),
        }
        parameters_hash = canonical_sha256(strategy_parameters)
        payload = {
            "schema_version": "1.0.0",
            "fitter_version": self.version,
            "candidate_version": candidate_version,
            "fold_index": fold_index,
            "train_dataset_hash": train_segment.dataset_hash,
            "training_rows": len(train_candles),
            "parameters_hash": parameters_hash,
            "diagnostics": diagnostics,
        }
        return WalkForwardFittedCandidate(
            **payload,
            artifact_hash=canonical_sha256(payload),
        )

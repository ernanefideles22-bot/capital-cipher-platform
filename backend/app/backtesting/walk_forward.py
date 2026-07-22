"""Deterministic, leakage-resistant walk-forward evaluation protocol."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import Protocol

from app.backtesting.acceptance import evaluate_walk_forward_gate
from app.backtesting.artifacts import (
    canonical_sha256,
    walk_forward_artifact_hash,
)
from app.backtesting.engine import BacktestingEngine
from app.backtesting.execution_data import (
    build_historical_execution_manifest,
)
from app.backtesting.fitting import (
    FrozenStrategyFitter,
    WalkForwardCandidateFitter,
)
from app.market_data.catalog import build_candle_dataset_manifest
from app.schemas.backtest import (
    BacktestReport,
    WalkForwardAggregate,
    WalkForwardBacktestSummary,
    WalkForwardFoldReport,
    WalkForwardProtocol,
    WalkForwardReport,
    WalkForwardRequest,
    WalkForwardSegment,
)
from app.schemas.market import Candle


class WalkForwardReportRepository(Protocol):
    async def save_walk_forward_report(
        self,
        report: WalkForwardReport,
    ) -> WalkForwardReport: ...

    async def load_walk_forward_report(
        self,
        experiment_id: str,
    ) -> WalkForwardReport | None: ...

    async def list_walk_forward_reports(
        self,
        *,
        limit: int = 100,
    ) -> list[WalkForwardReport]: ...


def _build_segment(
    role: str,
    ordered: list[Candle],
    start: int,
    end: int,
) -> WalkForwardSegment:
    selected = ordered[start:end]
    manifest = build_candle_dataset_manifest(selected)
    return WalkForwardSegment(
        role=role,
        start_index=start,
        end_index_exclusive=end,
        candles=len(selected),
        start_at=selected[0].closed_at,
        end_at=selected[-1].closed_at,
        dataset_id=manifest.dataset_id,
        dataset_hash=manifest.dataset_hash,
    )


def plan_walk_forward_windows(
    candles: list[Candle],
    protocol: WalkForwardProtocol,
) -> tuple[list[Candle], list[tuple[WalkForwardSegment, WalkForwardSegment, WalkForwardSegment]]]:
    """Create ordered, embargoed folds with non-overlapping test windows."""

    if not candles:
        raise ValueError("Walk-forward evaluation requires candles")

    ordered = sorted(candles, key=lambda candle: candle.closed_at)
    # The manifest validates one series and rejects duplicate timestamps.
    build_candle_dataset_manifest(ordered)

    windows: list[
        tuple[WalkForwardSegment, WalkForwardSegment, WalkForwardSegment]
    ] = []
    step = protocol.resolved_step_candles
    for fold_index in range(protocol.max_folds):
        if protocol.anchored_train:
            train_start = 0
            train_end = protocol.train_candles + fold_index * step
        else:
            train_start = fold_index * step
            train_end = train_start + protocol.train_candles

        validation_start = train_end + protocol.embargo_candles
        validation_end = validation_start + protocol.validation_candles
        test_start = validation_end + protocol.embargo_candles
        test_end = test_start + protocol.test_candles
        if test_end > len(ordered):
            break

        windows.append(
            (
                _build_segment("TRAIN", ordered, train_start, train_end),
                _build_segment(
                    "VALIDATION",
                    ordered,
                    validation_start,
                    validation_end,
                ),
                _build_segment("TEST", ordered, test_start, test_end),
            )
        )

    if not windows:
        minimum = (
            protocol.train_candles
            + protocol.validation_candles
            + protocol.test_candles
            + 2 * protocol.embargo_candles
        )
        raise ValueError(
            "Insufficient candles for one walk-forward fold: "
            f"requires at least {minimum}, received {len(ordered)}"
        )
    return ordered, windows


def _summary(report: BacktestReport) -> WalkForwardBacktestSummary:
    return WalkForwardBacktestSummary(
        dataset_id=report.dataset_id,
        dataset_hash=report.dataset_hash,
        candles_processed=report.candles_processed,
        total_trades=report.total_trades,
        net_pnl=report.net_pnl,
        net_pnl_percent=report.net_pnl_percent,
        expectancy=report.expectancy,
        max_drawdown=report.max_drawdown,
        fees=report.fees,
        slippage=report.slippage,
        funding=report.funding,
        liquidations=report.liquidations,
        liquidation_fees=report.liquidation_fees,
        total_execution_cost=report.total_execution_cost,
    )


def _aggregate(
    summaries: list[WalkForwardBacktestSummary],
) -> WalkForwardAggregate:
    net_percent = [item.net_pnl_percent for item in summaries]
    return WalkForwardAggregate(
        folds=len(summaries),
        total_trades=sum(item.total_trades for item in summaries),
        profitable_folds=sum(item.net_pnl > 0 for item in summaries),
        profitable_fold_ratio=round(
            sum(item.net_pnl > 0 for item in summaries) / len(summaries),
            4,
        ),
        mean_net_pnl_percent=round(statistics.fmean(net_percent), 4),
        median_net_pnl_percent=round(statistics.median(net_percent), 4),
        worst_max_drawdown=round(
            max(item.max_drawdown for item in summaries),
            4,
        ),
        mean_expectancy=round(
            statistics.fmean(item.expectancy for item in summaries),
            4,
        ),
        total_liquidations=sum(
            item.liquidations for item in summaries
        ),
    )


class WalkForwardEngine:
    """Evaluates a pre-registered candidate on isolated validation/test folds."""

    def __init__(
        self,
        backtesting_engine: BacktestingEngine,
        repository: WalkForwardReportRepository | None = None,
        fitter: WalkForwardCandidateFitter | None = None,
    ) -> None:
        self._backtesting_engine = backtesting_engine
        self._repository = repository
        self._fitter = fitter or FrozenStrategyFitter()
        self.reports: list[WalkForwardReport] = []

    async def list_reports(self, *, limit: int = 100) -> list[WalkForwardReport]:
        if self._repository is not None:
            return await self._repository.list_walk_forward_reports(limit=limit)
        return list(reversed(self.reports[-limit:]))

    async def get_report(
        self,
        experiment_id: str,
    ) -> WalkForwardReport | None:
        if self._repository is not None:
            return await self._repository.load_walk_forward_report(
                experiment_id
            )
        return next(
            (
                report
                for report in reversed(self.reports)
                if report.experiment_id == experiment_id
            ),
            None,
        )

    async def run(
        self,
        request: WalkForwardRequest,
        candles: list[Candle],
    ) -> WalkForwardReport:
        started = time.monotonic()
        ordered, windows = plan_walk_forward_windows(candles, request.protocol)
        full_manifest = build_candle_dataset_manifest(ordered)

        first = ordered[0]
        if (
            first.symbol != request.backtest.symbol.upper()
            or first.timeframe != request.backtest.timeframe
            or first.exchange.value != request.backtest.exchange.upper()
        ):
            raise ValueError(
                "Backtest request series does not match the candle dataset"
            )

        actual_candidate = self._backtesting_engine.resolve_strategy_version(
            request.backtest
        )
        if request.candidate_version != actual_candidate:
            raise ValueError(
                "candidate_version does not match the enabled strategy: "
                f"expected {actual_candidate}"
            )

        assumptions = self._backtesting_engine.resolve_execution_assumptions(
            request.backtest
        )
        simulation_context = self._backtesting_engine.simulation_context(
            request.backtest
        )
        simulation_context_hash = canonical_sha256(simulation_context)
        historical_execution_manifest = (
            build_historical_execution_manifest(
                request.backtest.historical_execution
            )
            if request.backtest.historical_execution is not None
            else None
        )
        identity_payload = {
            "dataset_hash": full_manifest.dataset_hash,
            "candidate_version": actual_candidate,
            "protocol": request.protocol.model_dump(mode="json"),
            "resolved_step_candles": request.protocol.resolved_step_candles,
            "execution_assumptions": assumptions.model_dump(mode="json"),
            "historical_execution_dataset_hash": (
                historical_execution_manifest.dataset_hash
                if historical_execution_manifest is not None
                else None
            ),
            "margin_assumptions": request.backtest.margin.model_dump(
                mode="json"
            ),
            "fitter_version": self._fitter.version,
            "research_plan": request.research_plan.model_dump(mode="json"),
            "acceptance": request.acceptance.model_dump(mode="json"),
            "simulation_context_hash": simulation_context_hash,
        }
        experiment_id = (
            f"walk-forward:v2:{canonical_sha256(identity_payload)}"
        )
        if self._repository is not None:
            existing = await self._repository.load_walk_forward_report(
                experiment_id
            )
            if existing is not None:
                return existing

        folds: list[WalkForwardFoldReport] = []
        validation_summaries: list[WalkForwardBacktestSummary] = []
        test_summaries: list[WalkForwardBacktestSummary] = []
        for fold_index, (train, validation, test) in enumerate(windows):
            fitted_candidate = self._fitter.fit(
                candidate_version=actual_candidate,
                fold_index=fold_index,
                train_segment=train,
                train_candles=ordered[
                    train.start_index : train.end_index_exclusive
                ],
                strategy_parameters=simulation_context["strategy"],
            )
            validation_report = await self._backtesting_engine.run(
                request.backtest,
                ordered[validation.start_index : validation.end_index_exclusive],
                record_report=False,
            )
            test_report = await self._backtesting_engine.run(
                request.backtest,
                ordered[test.start_index : test.end_index_exclusive],
                record_report=False,
            )
            validation_summary = _summary(validation_report)
            test_summary = _summary(test_report)
            validation_summaries.append(validation_summary)
            test_summaries.append(test_summary)

            fold_hash = canonical_sha256(
                {
                    "experiment_id": experiment_id,
                    "fold_index": fold_index,
                    "train_hash": train.dataset_hash,
                    "validation_hash": validation.dataset_hash,
                    "test_hash": test.dataset_hash,
                    "fitted_candidate_hash": (
                        fitted_candidate.artifact_hash
                    ),
                }
            )
            folds.append(
                WalkForwardFoldReport(
                    fold_id=f"walk-forward-fold:v2:{fold_hash}",
                    fold_index=fold_index,
                    train=train,
                    validation=validation,
                    test=test,
                    fitted_candidate=fitted_candidate,
                    validation_result=validation_summary,
                    test_result=test_summary,
                )
            )

        validation_aggregate = _aggregate(validation_summaries)
        test_aggregate = _aggregate(test_summaries)
        validation_gate = evaluate_walk_forward_gate(
            phase="VALIDATION",
            aggregate=validation_aggregate,
            criteria=request.acceptance,
            research_plan=request.research_plan,
        )
        test_gate = evaluate_walk_forward_gate(
            phase="TEST",
            aggregate=test_aggregate,
            criteria=request.acceptance,
            research_plan=request.research_plan,
        )
        report = WalkForwardReport(
            report_version="walk-forward-report-v2",
            experiment_id=experiment_id,
            artifact_hash="0" * 64,
            dataset_id=full_manifest.dataset_id,
            dataset_hash=full_manifest.dataset_hash,
            symbol=request.backtest.symbol,
            timeframe=request.backtest.timeframe,
            candidate_version=actual_candidate,
            protocol=request.protocol,
            resolved_step_candles=request.protocol.resolved_step_candles,
            execution_assumptions=assumptions,
            historical_execution_manifest=historical_execution_manifest,
            margin_assumptions=request.backtest.margin,
            research_plan=request.research_plan,
            acceptance_criteria=request.acceptance,
            fitter_version=self._fitter.version,
            simulation_context=simulation_context,
            simulation_context_hash=simulation_context_hash,
            folds=folds,
            validation_aggregate=validation_aggregate,
            test_aggregate=test_aggregate,
            validation_gate=validation_gate,
            test_gate=test_gate,
            research_decision=(
                "PASS"
                if validation_gate.passed and test_gate.passed
                else "FAIL"
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
            created_at=datetime.now(timezone.utc),
        )
        report = report.model_copy(
            update={"artifact_hash": walk_forward_artifact_hash(report)}
        )
        if self._repository is not None:
            report = await self._repository.save_walk_forward_report(report)
        if not any(
            item.experiment_id == report.experiment_id
            for item in self.reports
        ):
            self.reports.append(report)
        return report

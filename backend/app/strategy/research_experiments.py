"""Offline baseline experiment runner, binding established walk-forward to memory.

This runner measures the EXISTING, versioned StrategyEngine baseline only.
It does not execute an agent's new hypothesis, estimate its edge, or promote it.
No OMS, exchange adapters, broker credentials, schedulers or risk mutation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.backtesting.artifacts import walk_forward_artifact_hash
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import WalkForwardEngine
from app.schemas.backtest import WalkForwardRequest
from app.schemas.market import Candle
from app.strategy.research_lab import StrategyHypothesis
from app.strategy.research_memory import ResearchMemory


@dataclass(frozen=True)
class BaselineComparison:
    hypothesis_fingerprint: str
    baseline_version: str
    baseline_experiment_id: str
    baseline_report_sha256: str
    dataset_hash: str
    validation_passed: bool
    test_passed: bool
    baseline_total_test_trades: int
    conclusion: str = "BASELINE_ONLY_NOT_HYPOTHESIS_EDGE"
    execution_authorized: bool = False


class BaselineResearchRunner:
    def __init__(self, *, journal: ResearchMemory,
                 backtester: BacktestingEngine | None = None) -> None:
        self.journal = journal
        self.backtester = backtester or BacktestingEngine()

    async def run(
        self,
        hypothesis: StrategyHypothesis,
        request: WalkForwardRequest,
        candles: Sequence[Candle],
    ) -> BaselineComparison:
        if (hypothesis.symbol != request.backtest.symbol or
                hypothesis.timeframe != request.backtest.timeframe):
            raise ValueError("Hypothesis and backtest universe disagree")
        if request.backtest.source == "csv" or request.backtest.source == "store":
            # Caller supplies the immutable candle series explicitly; neither
            # a CSV path nor external store is read by this wrapper.
            if request.backtest.csv_path or request.backtest.candles:
                raise ValueError("Ambiguous explicit and external data sources")
        if not candles:
            raise ValueError("Empty historical series")
        assumed = self.backtester.resolve_execution_assumptions(request.backtest)
        minimum_per_side = (
            assumed.taker_fee_bps + assumed.half_spread_bps
            + assumed.base_slippage_bps
        )
        if minimum_per_side < 12.0:
            raise ValueError("Research cost assumptions below 12 bps/side")
        if request.backtest.margin.leverage != 1:
            raise ValueError("Research baseline requires 1x leverage")
        if request.protocol.embargo_candles < 1:
            raise ValueError("Research baseline requires positive embargo")
        if request.research_plan.candidate_budget < 1:
            raise ValueError("Pre-registered candidate budget required")

        baseline_version = self.backtester.resolve_strategy_version(request.backtest)
        if request.candidate_version != baseline_version:
            raise ValueError("Only the configured baseline strategy may run")
        # No database writes occur until the complete validation succeeds.
        runner = WalkForwardEngine(self.backtester, repository=None)
        report = await runner.run(request, list(candles))
        if report.artifact_hash != walk_forward_artifact_hash(report):
            raise ValueError("Baseline artifact integrity check failed")
        if report.dataset_hash == "" or not report.folds:
            raise ValueError("Invalid baseline experiment")
        if report.candidate_version != baseline_version:
            raise ValueError("Baseline candidate identity mismatch")
        result = BaselineComparison(
            hypothesis_fingerprint=hypothesis.fingerprint,
            baseline_version=baseline_version,
            baseline_experiment_id=report.experiment_id,
            baseline_report_sha256=report.artifact_hash,
            dataset_hash=report.dataset_hash,
            validation_passed=bool(report.validation_gate and report.validation_gate.passed),
            test_passed=bool(report.test_gate and report.test_gate.passed),
            baseline_total_test_trades=report.test_aggregate.total_trades,
        )
        # Audit is append-only. Baseline evidence is NOT OutOfSampleEvidence
        # for a new hypothesis and cannot authorize PAPER, Testnet or LIVE.
        self.journal.register(hypothesis)
        self.journal.append(
            kind="BASELINE_WALK_FORWARD_OBSERVED",
            hypothesis_hash=hypothesis.fingerprint,
            event_id="baseline:" + hypothesis.fingerprint + ":" + report.artifact_hash,
            payload={
                "hypothesis_fingerprint": result.hypothesis_fingerprint,
                "baseline_version": result.baseline_version,
                "baseline_experiment_id": result.baseline_experiment_id,
                "baseline_report_sha256": result.baseline_report_sha256,
                "dataset_hash": result.dataset_hash,
                "validation_passed": result.validation_passed,
                "test_passed": result.test_passed,
                "test_trades": result.baseline_total_test_trades,
                "purpose": "BASELINE_ONLY_NOT_HYPOTHESIS_EDGE",
                "execution_authorized": False,
            },
        )
        self.journal.verify_chain()
        return result

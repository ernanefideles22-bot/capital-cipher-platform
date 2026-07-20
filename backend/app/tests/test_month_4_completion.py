"""Month 4 completion: historical costs, margin, fitting, and gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.backtesting.acceptance import evaluate_walk_forward_gate
from app.backtesting.engine import BacktestingEngine
from app.backtesting.execution_data import HistoricalExecutionResolver
from app.backtesting.fitting import FrozenStrategyFitter
from app.backtesting.walk_forward import WalkForwardEngine
from app.core.config import Settings
from app.paper_trading.engine import PaperTradingEngine
from app.paper_trading.execution import (
    IsolatedMarginModel,
    RealisticExecutionModel,
)
from app.schemas.backtest import (
    BacktestExecutionAssumptions,
    BacktestMarginAssumptions,
    BacktestRequest,
    HistoricalExecutionDataset,
    HistoricalExecutionObservation,
    WalkForwardAcceptanceCriteria,
    WalkForwardAggregate,
    WalkForwardProtocol,
    WalkForwardResearchPlan,
    WalkForwardRequest,
)
from app.schemas.common import RiskStatus
from app.schemas.risk import RiskCheck
from app.schemas.strategy import StrategyConfig
from app.strategy.engine import StrategyEngine
from app.tests.conftest import make_candle, make_decision, make_series


def _historical_dataset(
    timestamps: list[datetime],
    *,
    spreads: list[float] | None = None,
    funding: list[float] | None = None,
    max_age_seconds: int = 3_600,
) -> HistoricalExecutionDataset:
    spreads = spreads or [1.0] * len(timestamps)
    funding = funding or [0.0] * len(timestamps)
    return HistoricalExecutionDataset(
        source="exchange-archive.test",
        exchange="BINANCE",
        symbol="BTCUSDT",
        max_age_seconds=max_age_seconds,
        observations=[
            HistoricalExecutionObservation(
                observed_at=at,
                half_spread_bps=spreads[index],
                funding_rate_bps_per_8h=funding[index],
                source_record_id=f"record-{index}",
            )
            for index, at in enumerate(timestamps)
        ],
    )


def test_historical_execution_is_as_of_only_and_integrates_funding():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    resolver = HistoricalExecutionResolver(
        _historical_dataset(
            [start, start + timedelta(hours=1)],
            spreads=[2.0, 7.0],
            funding=[8.0, 16.0],
        )
    )

    assert resolver.resolve(
        start + timedelta(minutes=59)
    ).half_spread_bps == 2.0
    assert resolver.resolve(
        start + timedelta(hours=1)
    ).half_spread_bps == 7.0
    assert resolver.funding_cost(
        position_notional=10_000,
        direction=1.0,
        start_at=start,
        end_at=start + timedelta(hours=2),
    ) == pytest.approx(3.0)
    with pytest.raises(ValueError, match="no observation"):
        resolver.resolve(start - timedelta(seconds=1))
    with pytest.raises(ValueError, match="stale"):
        resolver.resolve(start + timedelta(hours=2, seconds=1))


def test_historical_dataset_rejects_reordering_and_duplicates():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    later = start + timedelta(minutes=15)
    with pytest.raises(ValidationError, match="must be ordered"):
        _historical_dataset([later, start])
    with pytest.raises(ValidationError, match="duplicate times"):
        _historical_dataset([start, start])


def test_historical_spread_overrides_constant_assumption():
    at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    dataset = _historical_dataset([at], spreads=[25.0])
    model = RealisticExecutionModel(
        BacktestExecutionAssumptions(
            taker_fee_bps=0,
            half_spread_bps=1,
            base_slippage_bps=0,
            volume_impact_bps=0,
        ),
        historical_execution=HistoricalExecutionResolver(dataset),
    )
    candle = make_candle(close=100, closed_at=at)

    fill = model.open_fill(
        side="BUY",
        reference_price=100,
        position_notional=1_000,
        candle=candle,
    )

    assert fill.fill_price == pytest.approx(100.25)
    assert fill.spread_cost == pytest.approx(2.5)


async def test_backtest_records_historical_dataset_and_margin_manifest():
    candles = make_series(
        [100 * (1.001**index) for index in range(250)]
    )
    dataset = _historical_dataset(
        [candle.closed_at for candle in candles],
        spreads=[2.0 + (index % 3) for index in range(len(candles))],
        funding=[1.0] * len(candles),
        max_age_seconds=900,
    )
    request = BacktestRequest(
        historical_execution=dataset,
        margin=BacktestMarginAssumptions(leverage=2),
    )
    strategy = StrategyEngine(
        [
            StrategyConfig(
                strategy_id="MONTH_4_TEST",
                symbols=["BTCUSDT"],
                timeframe="15m",
                minimum_confidence=60,
            )
        ]
    )

    report = await BacktestingEngine(
        strategy_engine=strategy
    ).run(request, candles)

    assert report.historical_execution_manifest is not None
    assert report.historical_execution_manifest.row_count == len(candles)
    assert report.margin_assumptions.leverage == 2
    assert report.spread > 0
    assert report.funding > 0


async def test_liquidation_precedes_stop_and_charges_liquidation_fee(
    audit_service,
    risk_manager,
):
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    margin = BacktestMarginAssumptions(
        leverage=5,
        maintenance_margin_ratio=0.005,
        liquidation_fee_bps=10,
    )
    engine = PaperTradingEngine(
        audit_service,
        risk_manager,
        execution_model=RealisticExecutionModel(
            BacktestExecutionAssumptions(
                taker_fee_bps=0,
                half_spread_bps=0,
                base_slippage_bps=0,
                volume_impact_bps=0,
            )
        ),
        margin_model=IsolatedMarginModel(margin),
        started_at=start,
    )
    decision = make_decision()
    risk_manager.limits.risk_per_trade_percent = 0.5
    check = await risk_manager.check(
        decision,
        entry_price=100,
        atr=10 / 3,
        leverage=5,
    )
    order = await engine.create_order(
        decision,
        check,
        current_price=100,
        market_candle=make_candle(close=100, closed_at=start),
        occurred_at=start,
    )
    liquidation_candle = make_candle(
        open_=79,
        high=101,
        low=75,
        close=80,
        closed_at=start + timedelta(minutes=15),
    )

    closed = (await engine.on_candle(liquidation_candle))[0]
    performance = engine.performance()

    assert order.liquidation_price == pytest.approx(80.5)
    assert order.initial_margin == pytest.approx(200)
    assert closed.exit_reason == "LIQUIDATION"
    assert closed.exit_price == pytest.approx(79)
    assert closed.liquidation_fee == pytest.approx(0.79)
    assert performance.liquidations == 1
    assert performance.liquidation_fees_total == pytest.approx(0.79)


def _passing_aggregate(*, liquidations: int = 0) -> WalkForwardAggregate:
    return WalkForwardAggregate(
        folds=6,
        total_trades=60,
        profitable_folds=6,
        profitable_fold_ratio=1.0,
        mean_net_pnl_percent=2.0,
        median_net_pnl_percent=2.0,
        worst_max_drawdown=3.0,
        mean_expectancy=1.0,
        total_liquidations=liquidations,
    )


def test_acceptance_gate_applies_budget_and_liquidation_veto():
    criteria = WalkForwardAcceptanceCriteria()
    single_candidate = WalkForwardResearchPlan(candidate_budget=1)
    multi_candidate = WalkForwardResearchPlan(candidate_budget=4)

    passed = evaluate_walk_forward_gate(
        phase="TEST",
        aggregate=_passing_aggregate(),
        criteria=criteria,
        research_plan=single_candidate,
    )
    multiplicity_failed = evaluate_walk_forward_gate(
        phase="TEST",
        aggregate=_passing_aggregate(),
        criteria=criteria,
        research_plan=multi_candidate,
    )
    liquidation_failed = evaluate_walk_forward_gate(
        phase="TEST",
        aggregate=_passing_aggregate(liquidations=1),
        criteria=criteria,
        research_plan=single_candidate,
    )

    assert passed.passed is True
    assert passed.raw_sign_test_p_value == pytest.approx(0.015625)
    assert multiplicity_failed.passed is False
    assert multiplicity_failed.adjusted_p_value == pytest.approx(0.0625)
    assert liquidation_failed.passed is False
    assert any(
        "liquidations" in reason
        for reason in liquidation_failed.reasons
    )


def test_research_plan_fails_closed_when_index_exceeds_budget():
    with pytest.raises(ValidationError, match="candidate_budget"):
        WalkForwardResearchPlan(candidate_index=2, candidate_budget=1)


async def test_leverage_cannot_exceed_central_simulation_limit():
    with pytest.raises(ValidationError, match="DEFAULT_LEVERAGE"):
        Settings(DEFAULT_LEVERAGE=6, MAX_LEVERAGE_SIMULATED=5)

    with pytest.raises(ValueError, match="exceeds simulated risk limit"):
        await BacktestingEngine().run(
            BacktestRequest(
                margin=BacktestMarginAssumptions(leverage=6)
            ),
            [make_candle()],
        )


async def test_walk_forward_fitter_sees_train_only_and_stays_research_only():
    candles = make_series([100.0 + index for index in range(30)])

    class RecordingFitter(FrozenStrategyFitter):
        received: list = []

        def fit(self, **kwargs):
            self.received.append(kwargs["train_candles"])
            return super().fit(**kwargs)

    fitter = RecordingFitter()
    report = await WalkForwardEngine(
        BacktestingEngine(),
        fitter=fitter,
    ).run(
        WalkForwardRequest(
            candidate_version="SCALP_15M_v1",
            protocol=WalkForwardProtocol(
                train_candles=10,
                validation_candles=10,
                test_candles=10,
                embargo_candles=0,
                max_folds=1,
            ),
        ),
        candles,
    )

    assert fitter.received == [candles[:10]]
    assert report.experiment_id.startswith("walk-forward:v2:")
    assert report.folds[0].fitted_candidate is not None
    assert report.research_decision == "FAIL"
    assert report.promotion_status == "RESEARCH_ONLY"


def test_supabase_migration_is_private_append_only_and_rls_enabled():
    migration = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
        / "20260720065032_create_walk_forward_experiments.sql"
    ).read_text(encoding="utf-8")
    normalized = " ".join(migration.lower().split())

    assert "create schema if not exists capital_cipher" in normalized
    assert "generated always as identity" in normalized
    assert "report_payload jsonb not null" in normalized
    assert "enable row level security" in normalized
    assert "security invoker" in normalized
    assert "before update or delete" in normalized
    assert "revoke all on schema capital_cipher from public" in normalized

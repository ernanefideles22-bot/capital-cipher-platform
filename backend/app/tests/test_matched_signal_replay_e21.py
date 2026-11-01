"""E2.1 research-only matched execution and 3-core-agent capture invariants."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.matched_signal_replay_e21 import (
    SignalEvent, candidate_signal_events, matched_replay, native_signal_events,
)
from app.strategy.research_benchmark_e20 import (
    DATASETS, frozen_hypotheses, load_spot_csv,
)
from scripts.run_e21_matched_core_agent_benchmark import run

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "market_samples"


def bars(n=85):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        start_price = 100 + .2 * i
        closing = start + timedelta(minutes=15 * (i + 1))
        result.append(Candle(
            exchange=Exchange.BINANCE, symbol="BTCUSDT", timeframe="15m",
            open=start_price,
            high=start_price + .6, low=start_price - .2,
            close=start_price + .15, volume=100,
            closed_at=closing,
            received_at=closing + timedelta(seconds=1),
        ))
    return result


def test_matched_replay_never_uses_signal_bar_open():
    candles = bars()
    sig = SignalEvent(60, candles[60].received_at, "BUY", 0.5)
    report = matched_replay(candles, [sig], evaluation_start=60,
                            cost_bps_per_side=12)
    assert report["signals"] == 1
    assert report["entered"] == 1
    assert report["closed_including_replay_end"] == 1
    assert not report["execution_authorized"]
    assert report["common_model_id"] == "E21_NEXT_AVAILABLE_OPEN_STOP_FIRST_R2_V1"


def test_double_signal_and_stale_timestamp_fails_closed():
    candles = bars()
    sig = SignalEvent(60, candles[60].received_at, "BUY", 0.5)
    with pytest.raises(ValueError, match="one signal"):
        matched_replay(candles, [sig, sig], evaluation_start=60,
                       cost_bps_per_side=12)
    with pytest.raises(ValueError, match="lookahead"):
        matched_replay(candles, [
            SignalEvent(60, candles[60].closed_at, "BUY", 0.5),
        ], evaluation_start=60, cost_bps_per_side=12)
    with pytest.raises(ValueError, match="scenario"):
        matched_replay(candles, [sig], evaluation_start=60,
                       cost_bps_per_side=5)


def test_native_risk_subset_never_bypasses_approval():
    candles = bars()
    rows = [
        (60, "BUY", "BLOCKED"),
        (61, "BUY", "REDUCED"),
        (62, "SELL", "APPROVED"),
        (63, "WAIT", "PENDING"),
    ]
    all_events = native_signal_events(rows, candles, evaluation_start=60)
    accepted = native_signal_events(
        rows, candles, evaluation_start=60, only_risk_approved=True,
    )
    assert [x.side for x in all_events] == ["BUY", "BUY", "SELL"]
    assert [x.side for x in accepted] == ["BUY", "SELL"]
    with pytest.raises(ValueError, match="Duplicate native"):
        native_signal_events(rows + [(60, "BUY", "BLOCKED")],
                             candles, evaluation_start=60)


def test_common_cost_stress_cannot_improve_same_fills():
    candles = bars()
    events = [SignalEvent(60, candles[60].received_at, "BUY", 0.5)]
    low = matched_replay(candles, events, evaluation_start=60,
                         cost_bps_per_side=12)
    high = matched_replay(candles, events, evaluation_start=60,
                          cost_bps_per_side=25)
    assert low["entered"] == high["entered"]
    assert high["net_return_percent"] < low["net_return_percent"]
    assert high["total_execution_fees"] > low["total_execution_fees"]


def test_fixed_candidate_and_real_csv_have_same_evaluation_dates():
    for symbol, tf in DATASETS:
        candles, _ = load_spot_csv(
            FIXTURES / f"e20_{symbol}_{tf}_binance_spot_20261031.csv",
            symbol=symbol, timeframe=tf,
        )
        _, candidate = frozen_hypotheses(symbol, tf)
        events = candidate_signal_events(candidate, candles,
                                         evaluation_start=480)
        assert all(480 <= x.candle_index < 799 for x in events)
        assert all(x.available_at >= candles[x.candle_index].received_at
                   for x in events)


@pytest.mark.asyncio
async def test_native_core_decisions_vs_candidate_same_model_no_promotion():
    result = await run(FIXTURES)
    assert result["mode"] == "E21_MATCHED_SIGNAL_REPLAY_HISTORICAL_RESEARCH_ONLY"
    assert not result["prospective_out_of_sample"]
    assert not result["promotion_authorized"]
    assert result["deployed_agent_quality_improved"] == "NOT_ESTABLISHED"
    assert result["native_agents"].startswith("CORE_")
    assert len(result["benchmark_series"]) == 3
    for series in result["benchmark_series"]:
        assert series["native_decisions_observed"] > 0
        for scenario in series["scenarios"]:
            arms = (
                scenario["native_core_pre_risk_signals_shared_execution"],
                scenario["native_core_risk_approved_subset_shared_execution"],
                scenario["frozen_chart_candidate_shared_execution"],
            )
            assert len(set(a["cost_bps_per_side"] for a in arms)) == 1
            assert len(set(a["common_model_id"] for a in arms)) == 1
            assert len(set(a["bar_count"] for a in arms)) == 1
            assert all(not a["execution_authorized"] for a in arms)
            assert not scenario["quality_improvement_proven"]

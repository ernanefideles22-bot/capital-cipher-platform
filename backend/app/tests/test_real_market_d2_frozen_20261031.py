"""Frozen REAL Binance USD-M OHLCV smoke for the actual D2 Python engine.

Records are public futures candles read on 2026-10-31 via Binance connector,
not authenticated Gate records. Receipt time is ASSUMED close+1000ms, not
observed. Tests are not evidence of Gate-specific execution quality or alpha.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.schemas.backtest import WalkForwardProtocol, WalkForwardResearchPlan
from app.strategy.candidate_replay import replay_candidate
from app.strategy.candidate_walk_forward import evaluate_candidate_walk_forward
from app.strategy.research_lab import StrategyHypothesis
from app.strategy.research_memory import ResearchMemory


FIXTURES = [
    ("BTCUSDT", "15m", 900_000),
    ("ETHUSDT", "1h", 3_600_000),
    ("SOLUSDT", "4h", 14_400_000),
]
CLOSE_CUTOFF_MS = int(
    datetime(2026, 10, 31, 18, tzinfo=timezone.utc).timestamp() * 1000
)


def real_futures_bars(symbol: str, timeframe: str, interval_ms: int):
    fixture = Path(__file__).parent / "fixtures" / (
        f"research_20261031_{symbol.lower()}_{timeframe}.json"
    )
    source = json.loads(fixture.read_text(encoding="utf-8"))
    assert source["source"] == "Binance USD-M futures public KLINES (not Gate)"
    assert source["symbol"] == symbol
    assert source["interval"] == timeframe
    assert "ASSUMED" in source["received_at"]
    assert len(source["rows"]) == 1499
    observed = []
    for row in source["rows"]:
        start, open_, high, low, close, volume, close_ms = row
        assert close_ms == start + interval_ms - 1
        assert close_ms <= CLOSE_CUTOFF_MS
        assert math.isfinite(float(close))
        observed.append(Candle(
            exchange=Exchange.BINANCE, symbol=symbol,
            timeframe=timeframe, open=float(open_),
            high=float(high), low=float(low),
            close=float(close), volume=float(volume),
            closed_at=datetime.fromtimestamp(
                close_ms / 1000, timezone.utc
            ),
            received_at=datetime.fromtimestamp(
                (close_ms + 1000) / 1000, timezone.utc
            ),
        ))
    for prev, next_ in zip(source["rows"], source["rows"][1:]):
        assert next_[0] - prev[0] == interval_ms
    return observed


def candidate(symbol: str, timeframe: str, side: str) -> StrategyHypothesis:
    return StrategyHypothesis(
        author_agent="FrozenRealMarketResearchAgent",
        name=f"Frozen momentum {symbol} {timeframe} {side}",
        family="MOMENTUM_REGIME",
        symbol=symbol, timeframe=timeframe, side=side,
        entry_features=(
            ("trend_up", "momentum_positive") if side == "BUY"
            else ("trend_down", "momentum_negative")
        ),
        invalidation_feature=("trend_down" if side == "BUY" else "trend_up"),
        parameters=(("atr_stop_multiple", 1.5), ("target_r_multiple", 2.0)),
        thesis="Pre-registered trend momentum entry with fixed stop ATR and "
               "take-profit 2R, tested without future candle access.",
    )


@pytest.mark.parametrize("symbol,timeframe,interval_ms", FIXTURES)
def test_real_market_candidate_replay_no_orders_and_cost_stress(
    symbol, timeframe, interval_ms,
):
    candles = real_futures_bars(symbol, timeframe, interval_ms)
    split = int(len(candles) * .6)
    for side in ("BUY", "SELL"):
        hyp = candidate(symbol, timeframe, side)
        training = replay_candidate(
            hyp, candles[:split], evaluation_start=60,
            all_in_cost_bps_per_side=12,
        )
        test = replay_candidate(
            hyp, candles, evaluation_start=split,
            all_in_cost_bps_per_side=12,
        )
        stress = replay_candidate(
            hyp, candles, evaluation_start=split,
            all_in_cost_bps_per_side=25,
        )
        assert training.status == test.status == "RESEARCH_ONLY"
        assert not test.execution_authorized and not stress.execution_authorized
        assert test.dataset_hash == stress.dataset_hash
        assert test.all_in_cost_bps_per_side == 12
        assert all(t.entry_signal_received_at <= t.entry_at for t in test.trades)
        assert all(t.exit_signal_received_at <= t.exit_at for t in test.trades)
        assert all(t.execution_costs > 0 for t in test.trades)
        assert math.isfinite(test.net_pnl)
        assert test.total_costs >= 0
        print("REAL_FUTURES_D2_PYTHON_SMOKE " + json.dumps({
            "source": "Binance USD-M futures; NOT Gate",
            "symbol": symbol, "timeframe": timeframe,
            "direction": side, "candles": len(candles),
            "split_bar": split, "test_from": candles[split].closed_at.isoformat(),
            "training_trades": len(training.trades),
            "training_net_usdt": training.net_pnl,
            "oos_trades": len(test.trades),
            "oos_wins": sum(t.net_pnl > 0 for t in test.trades),
            "oos_net_usdt_12bps": test.net_pnl,
            "oos_net_usdt_25bps": stress.net_pnl,
            "oos_drawdown_pct": test.max_drawdown_percent,
            "execution_authorized": False,
        }, sort_keys=True))


@pytest.mark.parametrize("symbol,timeframe,interval_ms", FIXTURES)
def test_real_market_walkforward_candidate_is_research_only(
    symbol, timeframe, interval_ms, tmp_path,
):
    candles = real_futures_bars(symbol, timeframe, interval_ms)
    proposal = candidate(symbol, timeframe, "BUY")
    memory = ResearchMemory(tmp_path / "observations.sqlite3")
    result = evaluate_candidate_walk_forward(
        proposal, candles, journal=memory,
        protocol=WalkForwardProtocol(
            train_candles=250, validation_candles=100,
            test_candles=100, embargo_candles=2, max_folds=4,
        ),
        research_plan=WalkForwardResearchPlan(
            candidate_budget=20, candidate_index=1,
        ),
        all_in_cost_bps_per_side=12,
    )
    assert len(result.folds) == 4
    assert not result.execution_authorized
    assert result.status == "RESEARCH_ONLY"
    assert len(memory.list_events()) == 2
    assert memory.verify_chain() == memory.list_events()[-1].event_hash
    print("REAL_FUTURES_WALK_FORWARD " + json.dumps({
        "symbol": symbol,
        "timeframe": timeframe,
        "folds": len(result.folds),
        "test_trades": result.test_trades,
        "profitable_folds": result.profitable_test_folds,
        "p_corrected_exploratory": result.adjusted_sign_test_p,
        "median_test_return_pct": result.median_test_net_return_percent,
        "status": result.status,
    }, sort_keys=True))

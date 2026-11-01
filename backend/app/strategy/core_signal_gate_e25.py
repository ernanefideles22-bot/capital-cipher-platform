"""E2.5 preregistered SPOT long-only core-agent signal quality experiment.

Protocol and single filter were committed BEFORE June-2026 historical OHLCV
was retrieved. Disjoint from E2.0/E2.1 data, but historical, NOT prospective.
No production agent/OMS/risk changes and no execution authority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Sequence

from app.schemas.market import Candle
from app.strategy.market_intelligence import analyze_closed_candles
from app.strategy.matched_signal_replay_e21 import SignalEvent

EXPERIMENT = {
    "version": "E25_SPOT_NATIVE_CORE_LONG_TREND_GATE_FROZEN_V1",
    "pairs": [["BTCUSDT", "15m"], ["ETHUSDT", "1h"], ["SOLUSDT", "4h"]],
    "venue": "BINANCE_SPOT_LONG_ONLY",
    "last_candle_close_utc": "2026-06-01T00:00:00+00:00",
    "bars_per_series": 1000,
    "evaluation_start_index": 300,
    "native_baseline": "CURRENT_VERSIONED_THREE_CORE_AGENT_BUY_SIGNAL_ONLY",
    "candidate": "SAME_NATIVE_BUY_SIGNAL_PLUS_TREND_REGIME_AND_MOMENTUM_FILTER",
    "filter_definition": {
        "regime": "UPTREND",
        "price_vs_ema20": "last_close>=ema20",
        "momentum": "last_close>close_4_bars_back",
        "relative_volume_min": 0.8,
    },
    "simulation_model": "E21_NEXT_AVAILABLE_OPEN_STOP_FIRST_R2_V1",
    "fees_per_side_bps": [12.0, 25.0, 40.0],
    "min_closed_trades_per_arm_for_descriptive_evidence": 30,
    "min_closed_trades_per_arm_for_research_review": 100,
    "min_unseen_evaluation_folds_for_research_review": 3,
    "research_only": True,
    "automatic_promotion": False,
    "live_testnet_enabled": False,
}
PAIRS = tuple(tuple(x) for x in EXPERIMENT["pairs"])
COSTS = tuple(EXPERIMENT["fees_per_side_bps"])


def experiment_digest() -> str:
    return hashlib.sha256(json.dumps(
        EXPERIMENT, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def make_filtered_long_only(
    native_signals: Sequence[SignalEvent], bars: Sequence[Candle],
    *, evaluation_start: int,
) -> list[SignalEvent]:
    """One predeclared conjunctive gate; no learned parameters, no short fills."""
    if not 60 <= evaluation_start < len(bars) - 1:
        raise ValueError("Invalid evaluation boundary")
    output: list[SignalEvent] = []
    seen: set[int] = set()
    for event in native_signals:
        i = event.candle_index
        if event.side != "BUY":
            raise ValueError("Not a BINANCE SPOT long-only event")
        if i in seen or not evaluation_start <= i < len(bars) - 1:
            raise ValueError("Duplicate or non-evaluation native signal")
        seen.add(i)
        bar = bars[i]
        if event.available_at < bar.received_at:
            raise ValueError("Signal predates candle availability")
        history = bars[max(0, i-59):i+1]
        if len(history) != 60:
            raise ValueError("Required 60-candle warmup unavailable")
        snap = analyze_closed_candles(history, as_of=event.available_at)
        if (snap.regime == "UPTREND" and
            bar.close >= snap.ema20 and
            bar.close > bars[i-4].close and
            snap.relative_volume >= 0.8):
            output.append(event)
    return output

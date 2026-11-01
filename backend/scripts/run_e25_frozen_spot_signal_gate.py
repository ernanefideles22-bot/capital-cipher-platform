"""Run E2.5 frozen long-only market-regime experiment offline (no network).

Three-core-agent BacktestingEngine is the versioned historical baseline.
This does NOT assess each of the 300 runtime agents separately.
Data are a disjoint 2026-06-01 historical snapshot, not future/live.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backtesting.engine import BacktestingEngine
from app.schemas.backtest import BacktestRequest
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.core_signal_gate_e25 import (
    EXPERIMENT, PAIRS, COSTS, experiment_digest, make_filtered_long_only,
)
from app.strategy.matched_signal_replay_e21 import (
    matched_replay, native_signal_events,
)
from app.strategy.candidate_replay import BAR_SECONDS


def load_frozen_csv(path: Path, symbol: str, timeframe: str):
    if (symbol,timeframe) not in PAIRS:
        raise ValueError("Market not preregistered")
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != "open_time_ms,open,high,low,close,volume":
        raise ValueError("Noncanonical Binance spot CSV")
    data = list(csv.DictReader(lines))
    if len(data) != EXPERIMENT["bars_per_series"]:
        raise ValueError("Wrong fixed OHLCV row count")
    interval = timedelta(seconds=BAR_SECONDS[timeframe])
    cutoff = datetime.fromisoformat(EXPERIMENT["last_candle_close_utc"])
    bars = []
    prev = None
    for row in data:
        opened = datetime.fromtimestamp(int(row["open_time_ms"])/1000, tz=timezone.utc)
        closed = opened + interval
        if prev is not None and closed - prev != interval:
            raise ValueError("Non-contiguous/duplicate bars")
        prev = closed
        vals = tuple(float(row[k]) for k in ("open","high","low","close","volume"))
        if not all(math.isfinite(x) for x in vals):
            raise ValueError("Nonfinite candle")
        if not (
            vals[0]>0 and vals[1]>0 and vals[2]>0 and vals[3]>0 and
            vals[4]>=0 and vals[1]>=max(vals[0],vals[2],vals[3]) and
            vals[2]<=min(vals[0],vals[1],vals[3])
        ):
            raise ValueError("Invalid OHLCV values")
        bars.append(Candle(
            exchange=Exchange.BINANCE, symbol=symbol, timeframe=timeframe,
            open=vals[0], high=vals[1], low=vals[2],
            close=vals[3], volume=vals[4],
            closed_at=closed, received_at=closed+timedelta(seconds=1),
        ))
    if bars[-1].closed_at != cutoff:
        raise ValueError("Market snapshot cutoff mismatch")
    return bars,{
        "provider": "BINANCE_PUBLIC_SPOT_OHLCV",
        "rows":len(bars), "file_sha256":hashlib.sha256(raw).hexdigest(),
        "first_closed_at_utc":bars[0].closed_at.isoformat(),
        "last_closed_at_utc":bars[-1].closed_at.isoformat(),
        "receipt_clock_is_assumed_not_measured":True,
        "real_spreads_and_fills_not_observed":True,
    }


async def run(fixtures: Path) -> dict:
    results=[]
    for symbol,timeframe in PAIRS:
        path=fixtures / f"e25_{symbol}_{timeframe}_spot_cutoff_20260601.csv"
        bars,manifest=load_frozen_csv(path,symbol,timeframe)
        idx={b.closed_at:i for i,b in enumerate(bars)}
        decisions=[]
        def capture(bar,decision):
            decisions.append((
                idx[bar.closed_at],
                decision.candidate_action.value,
                decision.risk_status.value,
            ))
        report=await BacktestingEngine().run(
            BacktestRequest(
                symbol=symbol,timeframe=timeframe,exchange=Exchange.BINANCE,
            ),bars,record_report=False,decision_observer=capture,
        )
        boundary=EXPERIMENT["evaluation_start_index"]
        pre_risk=native_signal_events(
            decisions,bars,evaluation_start=boundary,
        )
        # Native SELL signals are intentionally excluded from BOTH arms:
        # Binance SPOT cannot open uncollateralized directional short positions.
        baseline=[event for event in pre_risk if event.side=="BUY"]
        filtered=make_filtered_long_only(
            baseline,bars,evaluation_start=boundary,
        )
        compared=[]
        for bps in COSTS:
            native=matched_replay(
                bars,baseline,evaluation_start=boundary,cost_bps_per_side=bps,
            )
            gated=matched_replay(
                bars,filtered,evaluation_start=boundary,cost_bps_per_side=bps,
            )
            compared.append({
                "cost_bps_per_side":bps,
                "baseline_core_buy":native,
                "frozen_trend_gated_core_buy":gated,
                "net_return_delta_percentage_points":round(
                    gated["net_return_percent"]-native["net_return_percent"],8,
                ),
                "candidate_beat_baseline_on_this_historical_sample":
                    gated["net_return_percent"]>native["net_return_percent"],
                "both_arms_at_least_30_trades":min(
                    native["closed_including_replay_end"],
                    gated["closed_including_replay_end"],
                )>=EXPERIMENT["min_closed_trades_per_arm_for_descriptive_evidence"],
                "automated_promotion_authorized":False,
            })
        results.append({
            "symbol":symbol,"timeframe":timeframe,
            "dataset_manifest":manifest,
            "native_engine_model_version":report.execution_assumptions.model_version,
            "native_decisions_observed":len(decisions),
            "baseline_buy_signals":len(baseline),
            "filtered_buy_signals":len(filtered),
            "native_sell_signals_excluded":sum(
                event.side=="SELL" for event in pre_risk
            ),
            "native_candidate_actions_on_test_bars":dict(sorted(Counter(
                action for i,action,risk in decisions if i>=boundary
            ).items())),
            "comparisons":compared,
        })
    return {
        "protocol":EXPERIMENT,"protocol_sha256":experiment_digest(),
        "mode":"E25_DISJOINT_HISTORICAL_EXPLORATION_NOT_PROSPECTIVE",
        "results":results,
        "only_three_core_agents_not_all_300":True,
        "same_execution_and_costs_for_both_arms":True,
        "predeclared_fixed_filter":True,
        "not_live_not_testnet_not_deployed":True,
        "strategy_promotion_authorized":False,
        "profitability_proven":False,
        "limitations":[
            "Historical data disjoint from E2.0 but not forward/prospective.",
            "This compares native core BUY signals vs SAME BUY signals gated, not all 300 runtime agents.",
            "Lower exposure and fewer trades can create smaller losses without predictive alpha.",
            "Both arms use simplified model fills, a 1-second assumed candle receipt, and assumed costs.",
            "Three timeframes have substantially different lookback spans and are not pooled.",
            "Single-market replays do not simulate a correlated multi-asset risk budget.",
            "Signal selection after viewing these results would contaminate this exploratory evaluation.",
        ],
    }


async def main():
    fixtures=Path(__file__).resolve().parents[1]/"fixtures"/"market_samples"
    result=await run(fixtures)
    print("E25_FROZEN_GATE_RESULT "+json.dumps(
        result,sort_keys=True,separators=(",",":"),allow_nan=False,
    ))
    print("E25_FROZEN_GATE_COMPLETE")

if __name__=="__main__":
    asyncio.run(main())

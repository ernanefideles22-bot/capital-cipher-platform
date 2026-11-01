"""E2.6 future campaign evaluator: does nothing until complete observed data exist.

Offline read-only on trusted local files, no DB/OMS/exchange/API/agent jobs.
The three-core-agent baseline cannot represent the entire 300-agent runtime.
A self-asserted receipt clock is not independent prospective attestation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app.backtesting.engine import BacktestingEngine
from app.schemas.backtest import BacktestRequest
from app.schemas.common import Exchange
from app.strategy.core_signal_gate_e25 import make_filtered_long_only
from app.strategy.matched_signal_replay_e21 import (
    matched_replay, native_signal_events,
)
from app.strategy.prospective_core_gate_e26 import (
    CAMPAIGN, MARKETS, BPS, campaign_sha256,
    load_causally_observed_candles,
)


async def evaluate(data_dir: Path, *, now: datetime) -> dict:
    start = datetime.fromisoformat(
        CAMPAIGN["first_evaluation_candle_closed_at_utc"]
    )
    end = datetime.fromisoformat(
        CAMPAIGN["evaluation_candle_end_exclusive_utc"]
    )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware actual clock required")
    if now.astimezone(timezone.utc) < end:
        raise ValueError("Campaign has not completed; no early peeking")
    results = []
    for symbol,tf in MARKETS:
        path = data_dir / f"e26_{symbol}_{tf}_observed.csv"
        bars,manifest = load_causally_observed_candles(
            path,symbol=symbol,timeframe=tf,now=now,
        )
        timestamps = {bar.closed_at:i for i,bar in enumerate(bars)}
        decisions = []

        def capture(bar, decision):
            decisions.append((
                timestamps[bar.closed_at],
                decision.candidate_action.value,
                decision.risk_status.value,
            ))

        # Run the versioned existing CORE 3-agent logic, not 300 agents.
        original = await BacktestingEngine().run(
            BacktestRequest(symbol=symbol,timeframe=tf,exchange=Exchange.BINANCE),
            bars,record_report=False,decision_observer=capture,
        )
        boundary = CAMPAIGN["warmup_candles_per_market"]
        native = native_signal_events(
            decisions,bars,evaluation_start=boundary,
        )
        baseline = [event for event in native if event.side=="BUY"]
        filtered = make_filtered_long_only(
            baseline,bars,evaluation_start=boundary,
        )
        comparisons=[]
        for bps in BPS:
            baseline_result=matched_replay(
                bars,baseline,evaluation_start=boundary,
                cost_bps_per_side=bps,
            )
            filtered_result=matched_replay(
                bars,filtered,evaluation_start=boundary,
                cost_bps_per_side=bps,
            )
            comparisons.append({
                "cost_bps_per_side":bps,
                "core_spot_buy":baseline_result,
                "filtered_core_spot_buy":filtered_result,
                "incremental_net_return_pp":round(
                    filtered_result["net_return_percent"] -
                    baseline_result["net_return_percent"],8,
                ),
                "both_arms_have_30_closed_trades":min(
                    baseline_result["closed_including_replay_end"],
                    filtered_result["closed_including_replay_end"],
                )>=CAMPAIGN["min_closed_trades_for_descriptive_report"],
                "can_promote":False,
            })
        results.append({
            "symbol":symbol,"timeframe":tf,"source":manifest,
            "core_model_version":original.execution_assumptions.model_version,
            "core_decisions":len(decisions),
            "core_buy_signals":len(baseline),
            "core_sell_signals_excluded":sum(
                event.side=="SELL" for event in native
            ),
            "filtered_buy_signals":len(filtered),
            "decisions_by_action":dict(Counter(
                action for i,action,risk in decisions if i>=boundary
            )),
            "comparisons":comparisons,
        })
    return {
        "mode":"E26_FUTURE_DATE_OFFLINE_REPLAY_SOURCE_NOT_INDEPENDENTLY_ATTESTED",
        "campaign_sha256":campaign_sha256(),
        "research_campaign":CAMPAIGN,
        "results":results,
        "all_300_agent_runtime_skill_tested":False,
        "true_live_collector_enabled":False,
        "forecasts_prospectively_sealed_by_independent_party":False,
        "positive_net_edge_proven":False,
        "live_testnet_or_paper_order_execution_authorized":False,
        "promotion_authorized":False,
        "caveats":[
            "The frozen date windows are future relative to protocol registration.",
            "CSV observation timestamps are supplied by the collector, but not independently attested or cryptographically sealed.",
            "Actual venue fills, spread, funding, and slippage are not validated.",
            "Research outputs are NOT proof of live profitable trading.",
            "Even a positive result requires independent forward verification, 3 folds, enough trades and human approval.",
            "The native engine contains only MarketData, Quant and Trend agents, not the full 300-runtime ensemble.",
        ],
    }


async def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--data-dir",type=Path,default=None,
                        help="Locally captured complete E2.6 observed CSV dataset")
    args=parser.parse_args()
    if args.data_dir is None:
        print("E26_CAMPAIGN_STATUS "+json.dumps({
            "status":"FROZEN_WAITING_FOR_INDEPENDENT_FUTURE_CANDLE_COLLECTION",
            "campaign_sha256":campaign_sha256(),
            "campaign":CAMPAIGN,
            "collection_running":False,
            "execution_authorized":False,
        },sort_keys=True,separators=(",",":"),allow_nan=False))
        return 0
    result=await evaluate(args.data_dir,now=datetime.now(timezone.utc))
    print("E26_OFFLINE_RESULT "+json.dumps(
        result,sort_keys=True,separators=(",",":"),allow_nan=False,
    ))
    return 0


if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))

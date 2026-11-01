"""E2.1 actual three-core-agent signals vs frozen E2.0 chart signals.

Research ONLY. Observe the existing BacktestingEngine's MarketData/Quant/Trend
decision path, then pass those observations through the SAME separate, frozen
cost/position/stop replay as the chart candidate. No OMS/broker/database access.
The 800-candle source was viewed in E2.0: this is a historical diagnostic,
NOT prospective validation or proof of deployment improvement.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from app.backtesting.engine import BacktestingEngine
from app.schemas.backtest import BacktestRequest
from app.schemas.common import Exchange
from app.strategy.matched_signal_replay_e21 import (
    candidate_signal_events, matched_replay, native_signal_events,
)
from app.strategy.research_benchmark_e20 import (
    COST_SCENARIOS, DATASETS, RESEARCH_PROTOCOL, frozen_hypotheses,
    load_spot_csv, protocol_hash,
)


async def run(fixtures: Path) -> dict:
    series = []
    for symbol, timeframe in DATASETS:
        path = fixtures / f"e20_{symbol}_{timeframe}_binance_spot_20261031.csv"
        candles, manifest = load_spot_csv(
            path, symbol=symbol, timeframe=timeframe,
        )
        lookup = {x.closed_at: i for i, x in enumerate(candles)}
        observed: list[tuple[int, str, str]] = []

        def sink(candle, decision):
            observed.append((
                lookup[candle.closed_at],
                decision.candidate_action.value,
                decision.risk_status.value,
            ))

        # This exact engine uses the EXISTING MarketDataAgent, QuantAgent,
        # TrendAgent, decision consolidation, centralized risk and PAPER model.
        # It does not instantiate all 300 virtual agents.
        native_report = await BacktestingEngine().run(
            BacktestRequest(
                symbol=symbol, timeframe=timeframe, exchange=Exchange.BINANCE,
            ), candles, record_report=False, decision_observer=sink,
        )
        boundary = int(len(candles) * (1 - RESEARCH_PROTOCOL["holdout_fraction"]))
        reference, candidate = frozen_hypotheses(symbol, timeframe)
        native_all = native_signal_events(
            observed, candles, evaluation_start=boundary,
        )
        native_approved = native_signal_events(
            observed, candles, evaluation_start=boundary,
            only_risk_approved=True,
        )
        candidate_events = candidate_signal_events(
            candidate, candles, evaluation_start=boundary,
        )
        arms = []
        for cost in COST_SCENARIOS:
            full = matched_replay(
                candles, native_all, evaluation_start=boundary,
                cost_bps_per_side=cost,
            )
            approved = matched_replay(
                candles, native_approved, evaluation_start=boundary,
                cost_bps_per_side=cost,
            )
            trial = matched_replay(
                candles, candidate_events, evaluation_start=boundary,
                cost_bps_per_side=cost,
            )
            arms.append({
                "all_in_bps_per_side": cost,
                "native_core_pre_risk_signals_shared_execution": full,
                "native_core_risk_approved_subset_shared_execution": approved,
                "frozen_chart_candidate_shared_execution": trial,
                "candidate_minus_native_pre_risk_net_return_pp": round(
                    trial["net_return_percent"] - full["net_return_percent"], 8,
                ),
                "candidate_minus_native_approved_net_return_pp": round(
                    trial["net_return_percent"] - approved["net_return_percent"], 8,
                ),
                "quality_improvement_proven": False,
                "minimum_sample_requirement_met": min(
                    full["closed_including_replay_end"],
                    trial["closed_including_replay_end"],
                ) >= 30,
            })
        counts = Counter((action, risk) for i, action, risk in observed
                         if i >= boundary)
        series.append({
            "symbol": symbol, "timeframe": timeframe,
            "spot_execution_feasibility": (
                "SYNTHETIC_SHORT_NOT_SPOT_EXECUTABLE"
                if candidate.side == "SELL" else
                "LONG_SPOT_RESEARCH_ONLY"
            ),
            "source_manifest": manifest,
            "frozen_e20_protocol_sha256": protocol_hash(),
            "market_data_bars": len(candles),
            "holdout_bar_start": boundary,
            "native_decisions_observed": len(observed),
            "native_holdout_action_risk_counts": [
                {"action": action, "risk_status": risk, "count": count}
                for (action, risk), count in sorted(counts.items())
            ],
            "native_engine_version": native_report.execution_assumptions.model_version,
            "native_engine_strategy": (
                f"SCALP_15M_v1" if timeframe == "15m" else
                f"DAY_1H_v1" if timeframe == "1h" else "SWING_4H_v1"
            ),
            "native_engine_unmatched_paper_diagnostic": {
                "total_trades": native_report.total_trades,
                "net_pnl": native_report.net_pnl,
                "blocked_by_risk": native_report.blocked_by_risk,
            },
            "candidate_fingerprint": candidate.fingerprint,
            "scenarios": arms,
        })
    return {
        "mode": "E21_MATCHED_SIGNAL_REPLAY_HISTORICAL_RESEARCH_ONLY",
        "data_previously_explored_in_e20": True,
        "prospective_out_of_sample": False,
        "native_agents": "CORE_MARKET_DATA_QUANT_TREND_ONLY_NOT_300_RUNTIME_AGENTS",
        "same_fills_costs_risk_position_model_for_arms": True,
        "native_risk_approval_path_dependent_on_original_native_paper": True,
        "native_backtest_original_execution_is_diagnostic_not_comparable": True,
        "benchmark_series": series,
        "execution_authorized": False,
        "deployed_agent_quality_improved": "NOT_ESTABLISHED",
        "promotion_authorized": False,
        "limitations": [
            "Native baseline is original three-core-agent BacktestingEngine logic, not the entire 300-agent runtime.",
            "Signal-level comparison uses the same simplified fills for each arm; it is not the deployed OMS fill model.",
            "Holdout was previously revealed in E2.0, so this is an exploratory retrospective benchmark.",
            "No point-in-time order book quotes, measured spread/latency/fees, funding, or independent data seal.",
            "Approvals originate from separate native-paper position state, not the shared executor.",
            "ETH SELL uses spot-market candles to simulate a short not executable directly on Binance SPOT.",
            "Sparse trade counts, flat PnL from inactivity and lower exposure do not establish superior skill.",
        ],
    }


async def main() -> int:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "market_samples"
    result = await run(fixtures)
    print("E21_MATCHED_RESULT " + json.dumps(
        result, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ))
    print("E21_MATCHED_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

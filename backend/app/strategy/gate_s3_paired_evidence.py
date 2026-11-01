"""Describe verified prospective-journal arms; never select or promote traders.

Numerical improvement, sample size and permission are separate facts. In
particular, losing less than baseline does not beat cash, and even a positive
descriptive sample does not prove repeatable economic advantage.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.strategy.gate_s2d_forward import MIN_POST_FREEZE_15M_BARS
from app.strategy.gate_s2_replay import ResearchRisk
from app.strategy.gate_s3_durable_research import S3JournalBlocked
from app.strategy.gate_s3_equity_valuation import read_only_equity_report

COMPARISON_VERSION = "S3_PAIRED_DESCRIPTIVE_EVIDENCE_V1"


def read_only_comparison_report(path: str | Path) -> dict:
    """One verified source snapshot; comparison uses the same per-arm clocks.

    At least 96 eligible 15m candles and >=30 closed trades in EACH arm are a
    preliminary sample floor, not a statistical or economic acceptance rule.
    A journal's stricter trade threshold is respected. No pooling markets,
    ranking, tuning, promotion, risk changes or exchange/OMS integration.
    """
    valuation = read_only_equity_report(path)
    minimum_trades = max(ResearchRisk().min_oos_evidence_trades,
                         valuation["risk_assumptions"]["min_oos_evidence_trades"])
    grouped = {}
    for account in valuation["accounts"]:
        grouped.setdefault((account["symbol"], account["strategy"]), {})[
            account["arm"]] = account
    pairs = []
    for (symbol, strategy), arms in sorted(grouped.items()):
        if set(arms) != {"STANDALONE", "S1_CONFLUENCE"}:
            raise S3JournalBlocked("Incomplete paired research accounts")
        baseline, filtered = arms["STANDALONE"], arms["S1_CONFLUENCE"]
        if any(baseline[k] != filtered[k] for k in (
                "initial_equity", "candle_closed_at_utc", "observation_received_at_utc")):
            raise S3JournalBlocked("Unmatched paired research window or capital")
        window = valuation["eligible_windows"][symbol]
        eligible = window["post_freeze_candles"]
        enough_bars = eligible >= MIN_POST_FREEZE_15M_BARS
        enough_trades = all(a["completed_trades"] >= minimum_trades
                            for a in (baseline, filtered))
        delta = round(filtered["total_net_exit_estimate"]-
                      baseline["total_net_exit_estimate"], 8)
        pnl = filtered["total_net_exit_estimate"]
        if pnl > 0 and delta > 0:
            result = "ABOVE_BASELINE_AND_CASH_IN_SAMPLE"
        elif pnl < 0 and delta > 0:
            result = "REDUCED_LOSS_STILL_BELOW_CASH"
        elif pnl == 0:
            result = "MATCHED_CASH"
        elif pnl > 0:
            result = "POSITIVE_WITHOUT_BASELINE_IMPROVEMENT"
        else:
            result = "BELOW_CASH_WITHOUT_BASELINE_IMPROVEMENT"
        reasons = []
        if not enough_bars:
            reasons.append("INSUFFICIENT_POST_FREEZE_CANDLES")
        if not enough_trades:
            reasons.append("INSUFFICIENT_CLOSED_TRADES_PER_ARM")
        pairs.append({
            "symbol": symbol, "strategy": strategy, "entry_timeframe": "15m",
            "window": dict(window), "baseline": dict(baseline),
            "confluence": dict(filtered), "cash_benchmark_pnl": 0.,
            "estimated_delta_vs_baseline": delta,
            "estimated_delta_vs_cash": pnl,
            "drawdown_reduction_percentage_points": round(
                baseline["candle_close_max_drawdown_pct"]-
                filtered["candle_close_max_drawdown_pct"], 6),
            "descriptive_outcome": result,
            "evidence_status": "PRELIMINARY_DESCRIPTIVE_SAMPLE" if not reasons
                               else "INSUFFICIENT_EVIDENCE",
            "insufficiency_reasons": reasons,
            "closed_trade_sample_floor_met": enough_trades,
            "post_freeze_candle_floor_met": enough_bars,
            "economic_edge_proven": False,
            "automatic_promotion_authorized": False,
            "order_submission_authorized": False,
        })
    return {
        "comparison_model": COMPARISON_VERSION,
        "status": "DESCRIPTIVE_RESEARCH_ONLY" if pairs else "NO_ELIGIBLE_ACCOUNTS",
        "comparisons": pairs, "source_observations": valuation["observations"],
        "minimum_post_freeze_15m_candles": MIN_POST_FREEZE_15M_BARS,
        "minimum_closed_trades_each_arm": minimum_trades,
        "valuation": valuation,
        "statistical_significance_established": False,
        "independently_attested_receipt_times": False,
        "portfolio_performance_proven": False,
        "profitability_proven": False, "automatic_promotion_authorized": False,
        "execution_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="Existing isolated S3 research SQLite journal")
    args = parser.parse_args()
    try:
        result = read_only_comparison_report(args.journal)
    except (S3JournalBlocked, OSError) as exc:
        parser.exit(2, f"S3 evidence unavailable: {exc}\n")
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

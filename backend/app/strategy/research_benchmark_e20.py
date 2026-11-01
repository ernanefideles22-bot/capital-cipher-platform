"""E2.0 fixed, offline spot-market research benchmark for new trader skills.

Compare a frozen simple momentum reference and a fixed chart-reading candidate
on the SAME historical candles (not the deployed Capital Cipher agents).
Candles must be independently sourced with provenance. This module neither
modifies agents nor routes orders, and does NOT promote strategies.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.candidate_replay import replay_candidate
from app.strategy.research_lab import StrategyHypothesis

DATASETS = (
    ("BTCUSDT", "15m"),
    ("ETHUSDT", "1h"),
    ("SOLUSDT", "4h"),
)
COST_SCENARIOS = (12.0, 25.0)
TIMEFRAME_STEP = {"15m": 900, "1h": 3600, "4h": 14400}
RESEARCH_PROTOCOL = {
    "version": "E2.0_SPOT_CHART_SKILLS_FROZEN_V1",
    "source": "BINANCE_SPOT_OHLCV_PUBLIC",
    "pairs": [[s, tf] for s, tf in DATASETS],
    "data_end_exclusive_utc": "2026-10-31T00:00:00+00:00",
    "expected_rows_each": 800,
    "baseline_reference": "ONE_SIDE_FOUR_BAR_MOMENTUM_NOT_DEPLOYED_AGENT",
    "candidate": "PREDEFINED_TREND_PULLBACK_BREAKOUT_V1",
    "holdout_fraction": 0.40,
    "initial_equity_per_independent_trial": 10000.0,
    "risk_fraction": 0.005,
    "cost_bps_per_side": list(COST_SCENARIOS),
    "min_closed_trades_for_any_claim": 30,
    "execution_permission": False,
}


def protocol_hash() -> str:
    return hashlib.sha256(
        json.dumps(RESEARCH_PROTOCOL, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def frozen_hypotheses(symbol: str, timeframe: str) -> tuple[StrategyHypothesis, StrategyHypothesis]:
    if (symbol, timeframe) not in DATASETS:
        raise ValueError("Unregistered benchmark pair/timeframe")
    side = "SELL" if timeframe == "1h" else "BUY"
    opposite_momentum = "momentum_positive" if side == "SELL" else "momentum_negative"
    moment = "momentum_negative" if side == "SELL" else "momentum_positive"
    if timeframe == "15m":
        family = "TREND_PULLBACK"
        candidate_features = ("trend_up", "pullback_ema20", "momentum_positive")
    elif timeframe == "1h":
        family = "BREAKOUT_VOLUME"
        candidate_features = ("trend_down", "breakout_down", "volume_confirmation")
    else:
        family = "BREAKOUT_VOLUME"
        candidate_features = ("trend_up", "breakout_up", "volume_confirmation")
    shared = dict(symbol=symbol, timeframe=timeframe, side=side,
                  invalidation_feature=opposite_momentum,
                  parameters=(("atr_stop_multiple", 1.5),
                              ("target_r_multiple", 2.0),
                              ("volume_ratio_min", 1.2),
                              ("cooldown_bars", 2.0)))
    return (
        StrategyHypothesis(
            author_agent="FrozenMomentumReference",
            name="Frozen four candle momentum reference",
            family="MOMENTUM_REGIME",
            entry_features=(moment,),
            thesis="Research-only directional momentum reference; not the deployed agents and not optimized against this holdout.",
            **shared,
        ),
        StrategyHypothesis(
            author_agent="FrozenChartReadingCandidate",
            name="Frozen chart regime and confirmation candidate",
            family=family,
            entry_features=candidate_features,
            thesis="Require a closed candle trend/regime confirmation and either an EMA pullback or volume-confirmed breakout before entry.",
            **shared,
        ),
    )


def load_spot_csv(path: Path, *, symbol: str, timeframe: str) -> tuple[list[Candle], dict]:
    if (symbol, timeframe) not in DATASETS:
        raise ValueError("Unregistered series")
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != "open_time_ms,open,high,low,close,volume":
        raise ValueError("Non-canonical OHLCV CSV header")
    rows = list(csv.DictReader(lines))
    if len(rows) != RESEARCH_PROTOCOL["expected_rows_each"]:
        raise ValueError("Invalid fixed historical sample length")
    step_ms = TIMEFRAME_STEP[timeframe] * 1000
    end = datetime.fromisoformat(RESEARCH_PROTOCOL["data_end_exclusive_utc"])
    bars = []
    prev = None
    for row in rows:
        timestamp = int(row["open_time_ms"])
        if prev is not None and timestamp != prev + step_ms:
            raise ValueError("Missing, duplicate or unsorted real candle")
        prev = timestamp
        opened = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
        closed = opened + timedelta(milliseconds=step_ms)
        if closed > end:
            raise ValueError("Dataset extends beyond preregistered cutoff")
        prices = tuple(float(row[k]) for k in ("open", "high", "low", "close", "volume"))
        if not all(math.isfinite(x) for x in prices):
            raise ValueError("Nonfinite market data")
        bars.append(Candle(
            exchange=Exchange.BINANCE,
            symbol=symbol, timeframe=timeframe,
            open=prices[0], high=prices[1], low=prices[2],
            close=prices[3], volume=prices[4],
            closed_at=closed,
            # Conservative data availability: received 1 second after close.
            # Assumed for replay, not a verified exchange receipt timestamp.
            received_at=closed + timedelta(seconds=1),
        ))
    return bars, {
        "source": "BINANCE_SPOT_PUBLIC_OHLCV",
        "csv_sha256": hashlib.sha256(raw).hexdigest(),
        "candles": len(bars),
        "start_utc": bars[0].closed_at.isoformat(),
        "end_utc": bars[-1].closed_at.isoformat(),
        "receipt_time_verified": False,
        "order_book_spread_verified": False,
    }


def _stats(report) -> dict:
    closed = list(report.trades)
    positives = sum(t.net_pnl for t in closed if t.net_pnl > 0)
    losses = -sum(t.net_pnl for t in closed if t.net_pnl < 0)
    return {
        "closed_trades_including_replay_end": len(closed),
        "wins": sum(t.net_pnl > 0 for t in closed),
        "net_pnl": report.net_pnl,
        "net_return_percent": report.net_return_percent,
        "profit_factor": round(positives / losses, 6) if losses else None,
        "profit_factor_status": "NO_LOSSES_UNDEFINED" if not losses else "FINITE",
        "max_drawdown_percent": report.max_drawdown_percent,
        "all_in_costs": report.total_costs,
        "skipped_stale_entries": report.skipped_stale_entries,
        "data_hash": report.dataset_hash,
        "first_test_candle": report.evaluation_starts_at.isoformat(),
        "last_test_candle": report.evaluation_ends_at.isoformat(),
    }


def benchmark_series(path: Path, *, symbol: str, timeframe: str) -> dict:
    bars, source = load_spot_csv(path, symbol=symbol, timeframe=timeframe)
    boundary = int(len(bars) * (1 - RESEARCH_PROTOCOL["holdout_fraction"]))
    reference, candidate = frozen_hypotheses(symbol, timeframe)
    comparisons = []
    for costs in COST_SCENARIOS:
        baseline = replay_candidate(reference, bars, evaluation_start=boundary,
                                    all_in_cost_bps_per_side=costs)
        trial = replay_candidate(candidate, bars, evaluation_start=boundary,
                                 all_in_cost_bps_per_side=costs)
        comparisons.append({
            "bps_per_side": costs,
            "reference": _stats(baseline),
            "candidate": _stats(trial),
            "difference_net_return_pct_points": round(
                trial.net_return_percent - baseline.net_return_percent, 8
            ),
            "positive_improvement_proven": False,
            "min_trades_threshold_satisfied": (
                len(trial.trades) >= RESEARCH_PROTOCOL["min_closed_trades_for_any_claim"]
                and len(baseline.trades) >= RESEARCH_PROTOCOL["min_closed_trades_for_any_claim"]
            ),
        })
    return {
        "mode": "OFFLINE_INDEPENDENT_HISTORICAL_RESEARCH_ONLY",
        "protocol_sha256": protocol_hash(),
        "source": source,
        "symbol": symbol, "timeframe": timeframe,
        "holdout_start_index": boundary,
        "reference_fingerprint": reference.fingerprint,
        "candidate_fingerprint": candidate.fingerprint,
        "comparisons": comparisons,
        "current_deployed_agent_comparison_performed": False,
        "prospective_forward_validation_performed": False,
        "caveats": [
            "Historical holdout was not externally sealed and cannot prove live profitability.",
            "Reference is a fixed momentum rule, NOT the current deployed agent ensemble.",
            "Only post-receipt next-open fills are permitted; receipt delay is assumed, not verified.",
            "OHLCV cannot reconstruct intrabar order-book conditions or true slippage.",
            "The two 12/25-bps-per-side assumptions are scenario tests, not measured exchange costs.",
            "Independent one-symbol trials cannot be summed as a realistic correlated portfolio.",
            "No operational changes, automated learning or strategy promotion.",
        ],
    }

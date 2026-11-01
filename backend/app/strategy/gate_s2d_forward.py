"""Frozen-policy forward market evaluation for Gate S2C confluence, RESEARCH ONLY.

The policy was committed before the 2026-11-02 UTC boundary. Market candles
strictly AFTER that time are eligible for future observations. Public OHLCV
fetched in one later batch does not prove historical real-time API reception,
order fills or economic edge. This module never promotes strategies to OMS.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from app.schemas.market import Candle
from app.schemas.common import Exchange
from app.strategy.gate_classical_s2 import (
    MIN_HISTORY, STRATEGIES, _check_bars,
)
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment
from app.strategy.gate_s2c_confluence import HistoricalConfluenceEvaluator

POLICY_ID="S2C_MTF_TREND_RANGE_V1_LOCKED_20261101"
POLICY_SOURCE_COMMIT="8813431f2d80408241e9befe2a25ab4c5c0245a5"
FROZEN_AT=datetime(2026,11,2,0,0,tzinfo=timezone.utc)
MIN_POST_FREEZE_15M_BARS=96


@dataclass(frozen=True)
class ForwardPair:
    strategy_id: str
    baseline_trades: int
    filtered_trades: int
    baseline_completed_trades: int
    filtered_completed_trades: int
    baseline_net_pnl: float
    filtered_net_pnl: float
    baseline_drawdown_pct: float
    filtered_drawdown_pct: float
    difference_vs_baseline: float
    difference_vs_no_trade: float
    rejected_proposals: int
    enough_closed_filtered_trades: bool
    economic_edge_proven: bool = False
    order_submission_authorized: bool = False


@dataclass(frozen=True)
class ForwardEvaluation:
    policy_id: str
    policy_source_commit: str
    frozen_at: datetime
    symbol: str
    evaluated_at: datetime
    newest_closed_15m: datetime
    post_freeze_bars: int
    status: str
    comparisons: tuple[ForwardPair,...]
    cash_benchmark_pnl: float = 0.
    historical_realtime_receipts_proven: bool = False
    profitability_proven: bool = False
    automatic_promotion_authorized: bool = False
    order_submission_authorized: bool = False


def evaluate_frozen_forward(
    by_timeframe: Mapping[str,Sequence[Candle]], *,
    observed_at: datetime,
    friction: Friction = Friction(),
    risk: ResearchRisk = ResearchRisk(),
) -> ForwardEvaluation:
    """An unchanged S2C policy evaluated only after its precommitted freeze.

    This is a prospective-policy / retrospective batch-data evaluation, not
    paper-execution results. Market frames need historical warm-up candles.
    Missing enough future bars returns an explicit NOT_MATURE status.
    """
    if set(by_timeframe)!={"15m","1h","4h"}:
        raise ValueError("All frozen-policy market frames are mandatory")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Observation timestamp must be timezone-aware")
    observed_at=observed_at.astimezone(timezone.utc)
    primary=tuple(by_timeframe["15m"])
    if len(primary)<MIN_HISTORY+2:
        raise ValueError("Insufficient warm-up bars")
    if len(primary)>6000:
        raise ValueError("Unbounded historical batch")
    symbol=primary[0].symbol
    for tf,bars in by_timeframe.items():
        if len(bars)<80 or len(bars)>6000:
            raise ValueError("Invalid time-frame history size")
        if any(
            b.exchange != Exchange.GATE or b.symbol!=symbol or
            b.timeframe!=tf or b.closed_at>observed_at or
            b.received_at<b.closed_at or b.received_at>observed_at
            for b in bars
        ):
            raise ValueError("Mismatched or unobserved historical market data")
        # Continuity and non-finite data validation, without inventing
        # historical receipt times. All bars must be closed.
        _check_bars(tuple(bars),
                    bars[-1].closed_at,
                    replay=True)
    if (observed_at-primary[-1].closed_at).total_seconds()>1800:
        raise ValueError("Stale present-day 15m Gate market snapshot")
    if observed_at<FROZEN_AT:
        return ForwardEvaluation(
            POLICY_ID,POLICY_SOURCE_COMMIT,FROZEN_AT,symbol,observed_at,
            primary[-1].closed_at,0,"PRECOMMITTED_WINDOW_NOT_STARTED",()
        )
    # The FIRST new decision bar must have closed after freeze; entry can
    # happen only on its FOLLOWING bar's open. Anything older is warm-up.
    start=next((i for i,c in enumerate(primary) if c.closed_at>FROZEN_AT),
               len(primary))
    post_freeze=len(primary)-start
    if start<MIN_HISTORY:
        return ForwardEvaluation(
            POLICY_ID,POLICY_SOURCE_COMMIT,FROZEN_AT,symbol,observed_at,
            primary[-1].closed_at,post_freeze,
            "PRE_FREEZE_WARMUP_EVICTED_OR_MISSING",()
        )
    if post_freeze<MIN_POST_FREEZE_15M_BARS:
        return ForwardEvaluation(
            POLICY_ID,POLICY_SOURCE_COMMIT,FROZEN_AT,symbol,observed_at,
            primary[-1].closed_at,post_freeze,
            "INSUFFICIENT_NEW_CLOSED_BARS",()
        )
    evaluator_higher={"1h":tuple(by_timeframe["1h"]),
                      "4h":tuple(by_timeframe["4h"])}
    pairings=[]
    for strategy_id in sorted(STRATEGIES):
        a=replay_segment(
            strategy_id,primary,start_index=start,
            end_index=len(primary),segment="LOCKED_FORWARD_BASELINE",
            friction=friction,risk=risk,
        )
        evaluator=HistoricalConfluenceEvaluator(evaluator_higher)
        b=replay_segment(
            strategy_id,primary,start_index=start,
            end_index=len(primary),segment="LOCKED_FORWARD_FILTERED",
            friction=friction,risk=risk,
            plan_evaluator=evaluator.evaluate,
        )
        completed_a=sum(t.exit_reason!="END_WINDOW_ESTIMATE" for t in a.trades)
        completed_b=sum(t.exit_reason!="END_WINDOW_ESTIMATE" for t in b.trades)
        pairings.append(ForwardPair(
            strategy_id=strategy_id,baseline_trades=len(a.trades),
            filtered_trades=len(b.trades),
            baseline_completed_trades=completed_a,
            filtered_completed_trades=completed_b,
            baseline_net_pnl=round(a.net_pnl,6),
            filtered_net_pnl=round(b.net_pnl,6),
            baseline_drawdown_pct=round(a.max_drawdown_pct,6),
            filtered_drawdown_pct=round(b.max_drawdown_pct,6),
            difference_vs_baseline=round(b.net_pnl-a.net_pnl,6),
            difference_vs_no_trade=round(b.net_pnl,6),
            rejected_proposals=evaluator.counts["ENTRIES_REJECTED"],
            enough_closed_filtered_trades=completed_b>=risk.min_oos_evidence_trades,
        ))
    status=("DESCRIPTIVE_FORWARD_SAMPLE" if any(
        p.enough_closed_filtered_trades for p in pairings)
        else "INSUFFICIENT_FORWARD_TRADES")
    return ForwardEvaluation(
        POLICY_ID,POLICY_SOURCE_COMMIT,FROZEN_AT,symbol,observed_at,
        primary[-1].closed_at,post_freeze,status,tuple(pairings),
    )

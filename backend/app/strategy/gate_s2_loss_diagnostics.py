"""S2B: read-only loss attribution and expanding walk-forward research.

No tuning, exchange credentials, OMS, orders, agent activation or deployment.
Training decisions use strictly earlier bars. Each out-of-sample fold represents
an independent hypothetical USD 1000 research account; fold PnLs are NOT a
continuous or compounded portfolio equity curve.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isclose, isfinite
from typing import Sequence

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import MIN_HISTORY, STRATEGIES
from app.strategy.gate_s2_replay import (
    Friction, ResearchRisk, ReplayReport, replay_segment,
)


@dataclass(frozen=True)
class LossAttribution:
    strategy_id: str
    trades: int
    winning_trades: int
    losing_trades: int
    exits: tuple[tuple[str,int], ...]
    raw_gross_before_slippage: float
    slippage_drag: float
    exchange_fees: float
    funding_estimate: float
    net_pnl: float
    net_expectancy_per_trade: float | None
    realized_gross_at_slipped_prices: float
    stopped_trades: int
    target_trades: int
    estimated_boundary_exits: int
    trading_authorized: bool = False


def attribute(report: ReplayReport, *, assumptions: Friction) -> LossAttribution:
    """Reconstruct hypothetical un-slipped prices from saved fills.

    Attribution is exact for a GIVEN set of saved simulated trades; changing
    friction can alter future position paths, so this is not a zero-fee backtest.
    """
    gross_without_slip=0.
    gross_slipped=0.
    fees=0.
    funding=0.
    exits=Counter()
    winners=0
    losers=0
    slip=assumptions.slippage_bps_side/10_000.
    for t in report.trades:
        direction=1 if t.side=="LONG" else -1 if t.side=="SHORT" else None
        if direction is None or t.quantity<=0:
            raise ValueError("Invalid research trade")
        if t.entry_price<=0 or t.exit_price<=0:
            raise ValueError("Invalid research fill")
        raw_entry=t.entry_price/(1+direction*slip)
        raw_exit=t.exit_price/(1-direction*slip)
        unsubsidized_gross=(raw_exit-raw_entry)*t.quantity*direction
        if not isclose(t.gross_pnl,
                       (t.exit_price-t.entry_price)*t.quantity*direction,
                       rel_tol=1e-8,abs_tol=1e-7):
            raise ValueError("Corrupted trade gross")
        expected_net=t.gross_pnl-t.fees_paid-t.funding_paid
        if not isclose(t.net_pnl,expected_net,rel_tol=1e-8,abs_tol=1e-7):
            raise ValueError("Corrupted trade net")
        if not all(isfinite(v) for v in (
                unsubsidized_gross,t.gross_pnl,t.fees_paid,
                t.funding_paid,t.net_pnl)):
            raise ValueError("Nonfinite attribution")
        gross_without_slip+=unsubsidized_gross
        gross_slipped+=t.gross_pnl
        fees+=t.fees_paid
        funding+=t.funding_paid
        exits[t.exit_reason]+=1
        winners+=t.net_pnl>0
        losers+=t.net_pnl<0
    drag=gross_without_slip-gross_slipped
    net=gross_without_slip-drag-fees-funding
    if not isclose(report.net_pnl,net,rel_tol=1e-8,abs_tol=1e-6):
        raise ValueError("Report/trade reconciliation failure")
    return LossAttribution(
        strategy_id=report.strategy_id,trades=len(report.trades),
        winning_trades=winners,losing_trades=losers,
        exits=tuple(sorted(exits.items())),
        raw_gross_before_slippage=round(gross_without_slip,6),
        slippage_drag=round(drag,6),
        exchange_fees=round(fees,6),funding_estimate=round(funding,6),
        net_pnl=round(net,6),
        net_expectancy_per_trade=round(net/len(report.trades),6)
        if report.trades else None,
        realized_gross_at_slipped_prices=round(gross_slipped,6),
        stopped_trades=sum(v for reason,v in exits.items()
                           if reason.startswith("STOP")),
        target_trades=sum(v for reason,v in exits.items()
                          if reason.startswith("TARGET")),
        estimated_boundary_exits=exits["END_WINDOW_ESTIMATE"],
    )


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    training_end_index: int
    oos_start_index: int
    oos_end_index: int
    selected_strategy: str | None
    eligible_train_strategies: int
    training_report: ReplayReport | None
    oos_report: ReplayReport | None
    oos_attribution: LossAttribution | None
    sufficient_oos_trades: bool
    selection_uses_future_bars: bool = False


@dataclass(frozen=True)
class WalkForwardDiagnostic:
    folds: tuple[WalkForwardFold,...]
    total_folds: int
    folds_with_selection: int
    folds_with_sufficient_oos_evidence: int
    oos_completed_trades: int
    oos_net_pnl_sum_separate_accounts: float
    all_evaluations_successful: bool
    evidence_insufficient: bool
    execution_authorized: bool = False
    automatic_promotion_authorized: bool = False
    profitability_proven: bool = False


def expanding_walk_forward(
    bars: Sequence[Candle], *,
    initial_train_bars: int = 400,
    oos_bars: int = 120,
    friction: Friction = Friction(),
    risk: ResearchRisk = ResearchRisk(),
) -> WalkForwardDiagnostic:
    """Expanding TRAIN and disjoint forward OOS windows.

    Folds are allowed to train on PREVIOUS folds' OOS later in time.
    That is a documented walk-forward protocol, not an untouched final holdout.
    All strategy IDs and eligibility thresholds are frozen before evaluation.
    """
    if initial_train_bars<MIN_HISTORY+30 or oos_bars<30:
        raise ValueError("Insufficient training or forward window")
    if len(bars)<initial_train_bars+oos_bars:
        raise ValueError("Not enough bars for a complete forward fold")
    folds=[]
    for train_end in range(initial_train_bars,len(bars)-oos_bars+1,oos_bars):
        end=train_end+oos_bars
        training=tuple(
            replay_segment(strategy_id,bars[:train_end],
                           start_index=MIN_HISTORY,end_index=train_end,
                           segment="WF_TRAIN",friction=friction,risk=risk)
            for strategy_id in sorted(STRATEGIES)
        )
        eligible=[]
        for report in training:
            completed=[t for t in report.trades
                       if t.exit_reason!="END_WINDOW_ESTIMATE"]
            if (len(completed)>=risk.min_selection_trades
                    and report.net_pnl>0
                    and report.profit_factor is not None
                    and report.profit_factor>1):
                eligible.append(report)
        chosen=(sorted(eligible,key=lambda x:(-x.net_pnl,x.strategy_id))[0]
                if eligible else None)
        # Only the frozen TRAIN winner enters this fold's OOS slice.
        observed=replay_segment(
            chosen.strategy_id,bars[:end],start_index=train_end,
            end_index=end,segment="WF_OOS",
            friction=friction,risk=risk) if chosen else None
        attribution=attribute(observed,assumptions=friction) if observed else None
        completed_oos=(sum(
            t.exit_reason!="END_WINDOW_ESTIMATE" for t in observed.trades)
            if observed else 0)
        folds.append(WalkForwardFold(
            fold=len(folds)+1,training_end_index=train_end,
            oos_start_index=train_end,oos_end_index=end,
            selected_strategy=chosen.strategy_id if chosen else None,
            eligible_train_strategies=len(eligible),
            training_report=chosen,oos_report=observed,
            oos_attribution=attribution,
            sufficient_oos_trades=completed_oos>=risk.min_oos_evidence_trades,
        ))
    total_trades=sum(
        sum(t.exit_reason!="END_WINDOW_ESTIMATE" for t in f.oos_report.trades)
        for f in folds if f.oos_report)
    return WalkForwardDiagnostic(
        folds=tuple(folds),total_folds=len(folds),
        folds_with_selection=sum(f.selected_strategy is not None for f in folds),
        folds_with_sufficient_oos_evidence=sum(f.sufficient_oos_trades for f in folds),
        oos_completed_trades=total_trades,
        oos_net_pnl_sum_separate_accounts=round(sum(
            f.oos_report.net_pnl for f in folds if f.oos_report),6),
        all_evaluations_successful=True,
        evidence_insufficient=not folds or any(
            f.selected_strategy is None or not f.sufficient_oos_trades
            for f in folds),
    )

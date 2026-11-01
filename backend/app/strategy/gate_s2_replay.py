"""S2 research-only next-open replay and frozen 60/40 OOS strategy selection.

Design constraints:
- Features at closed bar i; entry no sooner than bar i+1 OPEN.
- Stop/target after entry, worst-case stop when both touched inside one bar.
- Exchange writes, OMS access, registry changes and automatic promotion prohibited.
- Fees, slippage and funding are disclosed ASSUMPTIONS, not Gate actual fills.
- One position per market, no overlap or same-bar reentry, 1x notional cap.
- Historical data are batch snapshots; replay does not fabricate receipts.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite
from typing import Sequence

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import (
    MIN_HISTORY, STRATEGIES, _check_bars, evaluate_strategy,
)


@dataclass(frozen=True)
class Friction:
    fee_bps_side: float = 6.
    slippage_bps_side: float = 6.
    funding_bps_8h: float = 1.

    def __post_init__(self):
        if not all(isfinite(v) and 0 <= v <= 100 for v in
                   (self.fee_bps_side, self.slippage_bps_side, self.funding_bps_8h)):
            raise ValueError("Friction assumptions out of bounds")


@dataclass(frozen=True)
class ResearchRisk:
    initial_equity: float = 1000.
    risk_per_trade_pct: float = 0.25
    max_notional_pct: float = 100.
    max_daily_loss_pct: float = 2.
    max_drawdown_pct: float = 10.
    min_selection_trades: int = 5
    min_oos_evidence_trades: int = 30

    def __post_init__(self):
        fields=(self.initial_equity,self.risk_per_trade_pct,self.max_notional_pct,
                self.max_daily_loss_pct,self.max_drawdown_pct)
        if (not all(isfinite(v) for v in fields) or self.initial_equity<=0
                or not 0 < self.risk_per_trade_pct <= 1
                or not 0 < self.max_notional_pct <= 100
                or not 0 < self.max_daily_loss_pct <= 5
                or not 0 < self.max_drawdown_pct <= 20
                or self.min_selection_trades < 1 or self.min_oos_evidence_trades < 1):
            raise ValueError("Unsafe S2 research risk assumptions")


@dataclass(frozen=True)
class Trade:
    strategy_id: str
    side: str
    decision_bar_index: int
    entry_bar_index: int
    exit_bar_index: int
    entry_price: float
    exit_price: float
    quantity: float
    stop_price: float
    target_price: float
    gross_pnl: float
    fees_paid: float
    funding_paid: float
    net_pnl: float
    exit_reason: str


@dataclass(frozen=True)
class ReplayReport:
    strategy_id: str
    segment: str
    bars_in_segment: int
    trades: tuple[Trade,...]
    start_equity: float
    end_equity: float
    net_pnl: float
    return_pct: float
    win_rate: float | None
    profit_factor: float | None
    max_drawdown_pct: float
    estimated_end_window_exits: int
    blocked_new_entries: int
    source_receipts_replayed_as_if_original: bool = False
    execution_authorized: bool = False


@dataclass(frozen=True)
class OOSResult:
    selection_status: str
    selected_strategy: str | None
    train_reports: tuple[ReplayReport,...]
    oos_report: ReplayReport | None
    in_sample_end_index: int
    out_of_sample_start_index: int
    oos_evidence_sufficient: bool
    automatic_promotion_authorized: bool = False
    order_submission_authorized: bool = False
    replay_is_not_real_execution: bool = True


@dataclass
class _OpenPosition:
    strategy_id: str
    side: str
    direction: int
    decision_index: int
    entered_index: int
    entry: float
    quantity: float
    stop: float
    target: float


def _adverse_execution(raw_price: float, direction: int, *, opening: bool,
                       friction: Friction) -> float:
    # Long entry buys high and exit sells low. Short entry sells low and buys high.
    multiplier=(1 if opening else -1)*direction
    return raw_price*(1+multiplier*friction.slippage_bps_side/10_000)


def _exit_trigger(bar: Candle, position: _OpenPosition) -> tuple[float,str] | None:
    if position.direction==1:
        if bar.open<=position.stop:
            return bar.open,"STOP_GAP"
        if bar.open>=position.target:
            return position.target,"TARGET_GAP_LIMIT"
        if bar.low<=position.stop:
            return position.stop,"STOP_FIRST" if bar.high>=position.target else "STOP"
        if bar.high>=position.target:
            return position.target,"TARGET"
    else:
        if bar.open>=position.stop:
            return bar.open,"STOP_GAP"
        if bar.open<=position.target:
            return position.target,"TARGET_GAP_LIMIT"
        if bar.high>=position.stop:
            return position.stop,"STOP_FIRST" if bar.low<=position.target else "STOP"
        if bar.low<=position.target:
            return position.target,"TARGET"
    return None


def _summary(strategy_id,segment,bars_count,trades,start,finish,blocked,max_dd):
    wins=[t.net_pnl for t in trades if t.net_pnl>0]
    losses=[-t.net_pnl for t in trades if t.net_pnl<0]
    return ReplayReport(
        strategy_id=strategy_id,segment=segment,bars_in_segment=bars_count,
        trades=tuple(trades),start_equity=start,end_equity=finish,
        net_pnl=finish-start,return_pct=(finish/start-1)*100,
        win_rate=len(wins)/len(trades)*100 if trades else None,
        profit_factor=sum(wins)/sum(losses) if losses else
            (None if not wins else float("inf")),
        max_drawdown_pct=max_dd,
        estimated_end_window_exits=sum(t.exit_reason=="END_WINDOW_ESTIMATE" for t in trades),
        blocked_new_entries=blocked,
    )


def replay_segment(
    strategy_id: str, bars: Sequence[Candle], *, start_index: int,
    end_index: int, segment: str,
    friction: Friction = Friction(), risk: ResearchRisk = ResearchRisk(),
) -> ReplayReport:
    """Process only [start_index, end_index) entries/exit fills using the known past."""
    if strategy_id not in STRATEGIES:
        raise ValueError("Unregistered versioned strategy")
    if not MIN_HISTORY + 2 <= len(bars):
        raise ValueError("Insufficient historical research data")
    if not MIN_HISTORY <= start_index < end_index <= len(bars):
        raise ValueError("Invalid replay window")
    # Batch timestamp check (real receipts must not predate close).
    as_of=max(b.received_at for b in bars)
    _check_bars(bars, as_of, replay=True)
    spec=STRATEGIES[strategy_id]
    equity=risk.initial_equity
    initial=equity
    peak=equity
    max_dd=0.
    daily_start=equity
    day=None
    trades=[]
    pos=None
    blocked=0
    for j in range(start_index+1,end_index):
        bar=bars[j]
        current_day=bar.closed_at.astimezone(timezone.utc).date()
        if day!=current_day:
            day=current_day
            daily_start=equity
        closed_this_bar=False
        if pos is not None:
            # The time limit is observed at the next OPEN (not at an unknowable
            # historical close); exits have precedence over any new entries.
            if j-pos.entered_index>=spec.max_hold_bars:
                triggered=bar.open,"TIME_LIMIT_NEXT_OPEN"
            else:
                triggered=_exit_trigger(bar,pos)
            if triggered is not None:
                raw_exit,reason=triggered
                exit_price=_adverse_execution(raw_exit,pos.direction,opening=False,friction=friction)
                gross=(exit_price-pos.entry)*pos.quantity*pos.direction
                fees=(pos.entry+exit_price)*pos.quantity*friction.fee_bps_side/10_000
                elapsed=(bar.closed_at-bars[pos.entered_index].closed_at).total_seconds()
                funding_intervals=max(1,ceil(max(0.,elapsed)/(8*3600)))
                funding=(pos.entry*pos.quantity*friction.funding_bps_8h/10_000
                         *funding_intervals)
                net=gross-fees-funding
                equity+=net
                trades.append(Trade(
                    strategy_id=pos.strategy_id,side=pos.side,
                    decision_bar_index=pos.decision_index,
                    entry_bar_index=pos.entered_index,exit_bar_index=j,
                    entry_price=pos.entry,exit_price=exit_price,
                    quantity=pos.quantity,stop_price=pos.stop,target_price=pos.target,
                    gross_pnl=gross,fees_paid=fees,funding_paid=funding,
                    net_pnl=net,exit_reason=reason))
                pos=None
                closed_this_bar=True
                peak=max(peak,equity)
                max_dd=max(max_dd,(1-equity/peak)*100)
        if pos is not None or closed_this_bar:
            continue
        if (equity<=0 or equity < daily_start*(1-risk.max_daily_loss_pct/100)
                or equity < peak*(1-risk.max_drawdown_pct/100)):
            blocked+=1
            continue
        # Signal evaluated with strictly earlier bars. This simulated cutoff
        # deliberately does not pretend batch receipt clocks existed in the past.
        prefix=bars[:j]
        decision_at=prefix[-1].closed_at+timedelta(microseconds=1)
        plan=evaluate_strategy(strategy_id,prefix,as_of=decision_at,
                               historical_replay=True)
        if plan.action not in ("LONG","SHORT"):
            continue
        direction=1 if plan.action=="LONG" else -1
        entry=_adverse_execution(bar.open,direction,opening=True,friction=friction)
        distance=plan.stop_distance
        if distance is None or not isfinite(distance) or distance<=0:
            continue
        if entry-distance<=0:
            continue
        quantity=min(equity*risk.risk_per_trade_pct/100/distance,
                     equity*risk.max_notional_pct/100/entry)
        if not isfinite(quantity) or quantity<=0:
            continue
        stop=entry-direction*distance
        target=entry+direction*plan.target_distance
        pos=_OpenPosition(strategy_id,plan.action,direction,j-1,j,
                          entry,quantity,stop,target)
        # A bracket can be hit on the entry bar AFTER its open. In ambiguous
        # bar ranges, _exit_trigger gives the stop priority.
        triggered=_exit_trigger(bar,pos)
        if triggered is not None:
            raw_exit,reason=triggered
            exit_price=_adverse_execution(raw_exit,direction,opening=False,friction=friction)
            gross=(exit_price-entry)*quantity*direction
            fees=(entry+exit_price)*quantity*friction.fee_bps_side/10_000
            funding=entry*quantity*friction.funding_bps_8h/10_000
            net=gross-fees-funding
            equity+=net
            trades.append(Trade(strategy_id,plan.action,j-1,j,j,entry,exit_price,
                                quantity,stop,target,gross,fees,funding,net,reason))
            pos=None
            peak=max(peak,equity)
            max_dd=max(max_dd,(1-equity/peak)*100)
    if pos is not None:
        # Research-only forced ending mark. Not a tradable closing order.
        last=bars[end_index-1]
        exit_price=_adverse_execution(last.close,pos.direction,opening=False,friction=friction)
        gross=(exit_price-pos.entry)*pos.quantity*pos.direction
        fees=(pos.entry+exit_price)*pos.quantity*friction.fee_bps_side/10_000
        elapsed=(last.closed_at-bars[pos.entered_index].closed_at).total_seconds()
        funding=pos.entry*pos.quantity*friction.funding_bps_8h/10_000*max(1,ceil(elapsed/(8*3600)))
        net=gross-fees-funding
        equity+=net
        trades.append(Trade(pos.strategy_id,pos.side,pos.decision_index,
                            pos.entered_index,end_index-1,pos.entry,exit_price,
                            pos.quantity,pos.stop,pos.target,gross,fees,funding,
                            net,"END_WINDOW_ESTIMATE"))
        peak=max(peak,equity)
        max_dd=max(max_dd,(1-equity/peak)*100)
    return _summary(strategy_id,segment,end_index-start_index,
                    trades,initial,equity,blocked,max_dd)


def select_then_test_oos(
    bars: Sequence[Candle], *, friction: Friction = Friction(),
    risk: ResearchRisk = ResearchRisk(), train_fraction: float = .60,
) -> OOSResult:
    """Freeze one candidate using TRAIN ONLY; never optimize against the OOS slice."""
    if not .5 <= train_fraction <= .8:
        raise ValueError("Train fraction must be in [.5,.8]")
    if len(bars) < 200:
        raise ValueError("At least 200 bars required for exploratory split")
    split=int(len(bars)*train_fraction)
    if split<=MIN_HISTORY+2 or len(bars)-split<30:
        raise ValueError("Insufficient train or OOS window")
    reports=tuple(replay_segment(name,bars,start_index=MIN_HISTORY,
                                 end_index=split,segment="TRAIN",
                                 friction=friction,risk=risk)
                  for name in sorted(STRATEGIES))
    # A winner on TRAIN does not prove an edge. Guard on sample count,
    # non-marked fills and profits after declared friction.
    eligible=[report for report in reports
              if (len([t for t in report.trades
                       if t.exit_reason!="END_WINDOW_ESTIMATE"])>=risk.min_selection_trades
                  and report.net_pnl>0 and report.profit_factor is not None
                  and report.profit_factor>1)]
    if not eligible:
        return OOSResult("NO_IN_SAMPLE_EDGE",None,reports,None,
                         split,split,False)
    chosen=sorted(eligible,key=lambda r:(-r.net_pnl,r.strategy_id))[0]
    test=replay_segment(chosen.strategy_id,bars,start_index=split,
                        end_index=len(bars),segment="OOS",
                        friction=friction,risk=risk)
    completed=[t for t in test.trades
               if t.exit_reason!="END_WINDOW_ESTIMATE"]
    sufficient=len(completed)>=risk.min_oos_evidence_trades
    return OOSResult(
        "EXPLORATORY_OOS_ONLY" if sufficient else "OOS_INSUFFICIENT_TRADES",
        chosen.strategy_id,reports,test,split,split,sufficient,
    )

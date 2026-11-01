"""S2C research-only confluence of S1 professional chart context and S2 proposals.

Pure decision-policy module: no broker, keys, OMS, database, agent roster,
execution signal, strategy activation, or modifications of original S1 state.
Only the S2 research plan is conditionally filtered to WAIT.

Historical replays use hypothetical bar-close availability, *not* evidence
that Gate candles were received at their historical close timestamps.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Mapping, Sequence

from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import (
    ResearchPlan, STRATEGIES, evaluate_strategy,
)
from app.strategy.professional_gate_chart_stage1 import (
    BAR_SECONDS, MIN_BARS, MultiTimeframeReading,
    analyze_gate_multi_timeframe,
)

REQUIRED_FRAMES=("4h","1h","15m")
TREND_STRATEGIES=frozenset({
    "EMA_TREND_PULLBACK_V1","DONCHIAN_VOLUME_BREAKOUT_V1",
})
RANGE_STRATEGIES=frozenset({"RANGE_BOLLINGER_REVERSION_V1"})


@dataclass(frozen=True)
class ConfluenceVerdict:
    strategy_id: str
    symbol: str
    timeframe: str
    cutoff: datetime
    original_action: str
    filtered_action: str
    status: str
    reasons: tuple[str,...]
    higher_regimes: tuple[str,str]
    alignment: str
    proposed_stop_distance: float | None
    proposed_target_distance: float | None
    authority_to_trade: bool = False
    empirical_improvement_proven: bool = False


def evaluate_confluence(
    plan: ResearchPlan, chart: MultiTimeframeReading,
) -> ConfluenceVerdict:
    """Evaluate a time-locked Stage 1 reading against an unchanged Stage 2 plan.

    The data-quality preconditions deliberately raise rather than yield WAIT:
    stale, wrong-symbol, or future observations are invalid input, not markets.
    An accepted hypothesis is STILL only eligible for simulated research.
    """
    if (plan.execution_authorized is not False
            or plan.predictive_edge_proven is not False
            or chart.trading_authorized is not False):
        raise ValueError("Executable or untrusted confluence inputs forbidden")
    if (plan.strategy_id not in STRATEGIES
            or plan.strategy_version!=STRATEGIES[plan.strategy_id].version):
        raise ValueError("Unregistered strategy version")
    if plan.action not in {"WAIT","LONG","SHORT"}:
        raise ValueError("Invalid strategy action")
    if (plan.cutoff.tzinfo is None or plan.cutoff.utcoffset() is None
            or chart.as_of.tzinfo is None or chart.as_of.utcoffset() is None):
        raise ValueError("Naive cutoff forbidden")
    cutoff=plan.cutoff.astimezone(timezone.utc)
    if chart.as_of.astimezone(timezone.utc)!=cutoff:
        raise ValueError("Chart and strategy decision clocks disagree")
    if chart.exchange!="GATE" or chart.symbol!=plan.symbol:
        raise ValueError("Mixed symbol or venue in confluence")
    reading={r.timeframe:r for r in chart.readings}
    if len(chart.readings)!=3 or set(reading)!=set(REQUIRED_FRAMES):
        raise ValueError("Missing unique 4h/1h/15m contexts")
    if plan.timeframe not in reading:
        raise ValueError("Missing strategy's own chart timeframe")
    for tf,r in reading.items():
        if (r.exchange!="GATE" or r.symbol!=plan.symbol
                or r.as_of.astimezone(timezone.utc)!=cutoff
                or r.last_close_at.astimezone(timezone.utc)>cutoff
                or (cutoff-r.last_close_at).total_seconds()>2*BAR_SECONDS[tf]
                or r.trading_authorized is not False
                or r.predictive_edge_proven is not False
                or r.candles_analyzed<MIN_BARS):
            raise ValueError("Stale, unsafe, or mismatched Stage 1 reading")
    primary=reading[plan.timeframe]
    r4,r1=reading["4h"],reading["1h"]
    reasons=[]
    if plan.action=="WAIT":
        reasons.append("NO_S2_ENTRY_PROPOSAL")
    else:
        if (plan.stop_distance is None or plan.target_distance is None
                or not all(isfinite(x) and x>0 for x in
                           (plan.stop_distance,plan.target_distance))):
            raise ValueError("Unsafe stop or target hypothesis")
        direction=1 if plan.action=="LONG" else -1
        if plan.strategy_id in TREND_STRATEGIES:
            wanted="UPTREND" if direction==1 else "DOWNTREND"
            if r4.regime!=wanted or r1.regime!=wanted:
                reasons.append("HIGHER_TIMEFRAMES_NOT_ALIGNED")
            if primary.regime==("DOWNTREND" if direction==1 else "UPTREND"):
                reasons.append("PRIMARY_TIMEFRAME_CONTRADICTION")
        elif plan.strategy_id in RANGE_STRATEGIES:
            if r4.regime in ("UPTREND","DOWNTREND") or r1.regime in ("UPTREND","DOWNTREND"):
                reasons.append("HIGHER_TIMEFRAME_TREND_AGAINST_MEAN_REVERSION")
            if primary.regime!="RANGE_OR_TRANSITION":
                reasons.append("PRIMARY_NOT_RANGING")
        else:
            raise ValueError("Unrecognized strategy policy")
        if direction==1 and any(p in primary.candle_patterns
            for p in ("BEARISH_ENGULFING","SHOOTING_STAR_SHAPE")):
            reasons.append("ADVERSE_BEARISH_CANDLE")
        elif direction==-1 and any(p in primary.candle_patterns
            for p in ("BULLISH_ENGULFING","HAMMER_SHAPE")):
            reasons.append("ADVERSE_BULLISH_CANDLE")
        # No target should be located behind a confirmed technical obstacle,
        # if one is available. This optional gate can LOWER opportunity count.
        # Existing S2 ATR bracket is never expanded or silently rewritten.
        if direction==1 and primary.resistance is not None:
            if primary.resistance<=primary.ema20:
                # S/R and current close live in the same priced market,
                # but EMA is not an admissible substitute for entry price:
                # do not reject based on this proxy.
                pass
        # Confirmed pivot levels are descriptive, not predictive; the
        # historical comparison will test whether these filters help.
    action=plan.action if not reasons else "WAIT"
    return ConfluenceVerdict(
        strategy_id=plan.strategy_id,symbol=plan.symbol,
        timeframe=plan.timeframe,cutoff=cutoff,
        original_action=plan.action,filtered_action=action,
        status="ALLOW_RESEARCH" if action in {"LONG","SHORT"}
               else "REJECT_RESEARCH",
        reasons=tuple(reasons),
        higher_regimes=(r4.regime,r1.regime),
        alignment=chart.alignment,
        proposed_stop_distance=plan.stop_distance,
        proposed_target_distance=plan.target_distance,
    )


class HistoricalConfluenceEvaluator:
    """Construct point-in-time S1 research contexts from public historical bars.

    Synthetic receipt timestamps are *only* used inside this explicit
    historical-replay adapter. Live S1 must always use actual receipt clocks.
    """
    def __init__(self, higher_timeframes: Mapping[str,Sequence[Candle]]):
        if set(higher_timeframes)!={"1h","4h"}:
            raise ValueError("Both higher historical contexts are required")
        self._series={}
        self._closes={}
        for tf in ("1h","4h"):
            series=tuple(higher_timeframes[tf])
            if len(series)<MIN_BARS or any(x.timeframe!=tf for x in series):
                raise ValueError("Incomplete higher historical series")
            if any(x.received_at<x.closed_at for x in series):
                raise ValueError("Invalid received-before-closed provenance")
            self._series[tf]=series
            self._closes[tf]=tuple(x.closed_at for x in series)
        self.counts=Counter()

    @staticmethod
    def _hypothetical_history(series: Sequence[Candle]) -> tuple[Candle,...]:
        # Explicitly isolate this rewrite to backward-looking research:
        # historical API batch receipts must never be used as if timely.
        return tuple(
            x.model_copy(update={"received_at":
                x.closed_at+timedelta(microseconds=1)})
            for x in series
        )

    def _time_locked_chart(
        self, primary: Sequence[Candle], cutoff: datetime,
    ) -> MultiTimeframeReading:
        if primary[-1].closed_at>cutoff:
            raise ValueError("Future primary candle")
        groups={"15m":self._hypothetical_history(primary[-120:])}
        for tf in ("1h","4h"):
            idx=bisect_right(self._closes[tf],cutoff)
            if idx<MIN_BARS:
                raise ValueError("Insufficient known higher-timeframe history")
            groups[tf]=self._hypothetical_history(
                self._series[tf][max(0,idx-120):idx])
        return analyze_gate_multi_timeframe(groups,as_of=cutoff)

    def evaluate(self, strategy_id: str,
                 prefix: Sequence[Candle], cutoff: datetime) -> ResearchPlan:
        baseline=evaluate_strategy(
            strategy_id,prefix,as_of=cutoff,historical_replay=True)
        if baseline.action=="WAIT":
            self.counts["NO_BASE_ENTRY"]+=1
            return baseline
        chart=self._time_locked_chart(prefix,cutoff)
        verdict=evaluate_confluence(baseline,chart)
        self.counts["BASE_ENTRIES_EVALUATED"]+=1
        if verdict.filtered_action=="WAIT":
            self.counts["ENTRIES_REJECTED"]+=1
            for reason in verdict.reasons:
                self.counts[reason]+=1
            return replace(baseline,action="WAIT",
                           stop_distance=None,target_distance=None,
                           reasoning="CONFLUENCE_REJECTED: "+",".join(verdict.reasons))
        self.counts["ENTRIES_ALLOWED"]+=1
        return baseline

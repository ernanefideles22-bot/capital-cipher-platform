"""Stage 1: causal professional chart *reading* for Gate futures research.

No network, API keys, broker/OMS calls, order submission, agent activation or
strategy promotion. Patterns are descriptive hypotheses, NOT demonstrated alpha.
All calculations use candles actually closed AND received by as_of.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Mapping, Sequence

from app.agents.indicators import macd, rsi
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.market_intelligence import analyze_closed_candles

BAR_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
MIN_BARS = 80
MAX_BARS = 3000
PIVOT_LEFT = 2
PIVOT_RIGHT = 2


@dataclass(frozen=True)
class ConfirmedPivot:
    kind: str
    index: int
    price: float
    occurred_at: datetime
    confirmed_at: datetime


@dataclass(frozen=True)
class ProfessionalChartReading:
    exchange: str
    symbol: str
    timeframe: str
    as_of: datetime
    last_close_at: datetime
    candles_analyzed: int
    regime: str
    ema20: float
    ema50: float
    atr14: float
    relative_volume: float
    rsi14: float | None
    macd_histogram: float | None
    candle_patterns: tuple[str, ...]
    chart_patterns: tuple[str, ...]
    market_structure: str
    pivot_highs: tuple[ConfirmedPivot, ...]
    pivot_lows: tuple[ConfirmedPivot, ...]
    support: float | None
    resistance: float | None
    support_trend_slope_atr_per_bar: float | None
    resistance_trend_slope_atr_per_bar: float | None
    fibonacci_leg: str
    fibonacci_retracements: tuple[tuple[str, float], ...]
    breakout: str
    data_source_authenticated: bool = False
    predictive_edge_proven: bool = False
    trading_authorized: bool = False


@dataclass(frozen=True)
class MultiTimeframeReading:
    exchange: str
    symbol: str
    as_of: datetime
    alignment: str
    readings: tuple[ProfessionalChartReading, ...]
    trading_authorized: bool = False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware candle clock required")
    return value.astimezone(timezone.utc)


def _validated(bars: Sequence[Candle], as_of: datetime) -> tuple[str, str, datetime]:
    as_of = _utc(as_of)
    if not MIN_BARS <= len(bars) <= MAX_BARS:
        raise ValueError("Professional reader requires 80-3000 candles")
    first = bars[0]
    if first.exchange != Exchange.GATE:
        raise ValueError("Stage 1 accepts GATE research candles only")
    if first.timeframe not in BAR_SECONDS:
        raise ValueError("Unsupported timeframe for Gate research")
    step = timedelta(seconds=BAR_SECONDS[first.timeframe])
    previous_closed = None
    previous_received = None
    for bar in bars:
        if (bar.exchange, bar.symbol, bar.timeframe) != (
            Exchange.GATE, first.symbol, first.timeframe
        ):
            raise ValueError("Mixed exchange, market or timeframe")
        closed, received = _utc(bar.closed_at), _utc(bar.received_at)
        if received < closed:
            raise ValueError("Receipt before actual close is not admissible")
        if closed > as_of or received > as_of:
            raise ValueError("Future or not-yet-received candle (lookahead)")
        if previous_closed is not None:
            if closed - previous_closed != step:
                raise ValueError("Missing, duplicate or irregular candle")
            if received < previous_received:
                raise ValueError("Decreasing receipt clock")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if not all(isfinite(float(value)) for value in values):
            raise ValueError("Non-finite OHLCV")
        if (min(bar.open, bar.high, bar.low, bar.close) <= 0
                or bar.volume < 0 or
                bar.high < max(bar.open, bar.close, bar.low) or
                bar.low > min(bar.open, bar.close, bar.high)):
            raise ValueError("Invalid OHLCV")
        previous_closed, previous_received = closed, received
    assert previous_closed is not None
    if as_of - previous_closed > step * 2:
        raise ValueError("Stale latest candle")
    return first.symbol, first.timeframe, as_of


def _confirmed_pivots(bars: Sequence[Candle]) -> tuple[tuple[ConfirmedPivot,...],tuple[ConfirmedPivot,...]]:
    highs, lows = [], []
    for i in range(PIVOT_LEFT, len(bars) - PIVOT_RIGHT):
        left = bars[i-PIVOT_LEFT:i]
        right = bars[i+1:i+1+PIVOT_RIGHT]
        candidate = bars[i]
        confirmed = _utc(bars[i+PIVOT_RIGHT].received_at)
        if all(candidate.high > b.high for b in (*left,*right)):
            highs.append(ConfirmedPivot(
                "HIGH", i, candidate.high, _utc(candidate.closed_at), confirmed
            ))
        if all(candidate.low < b.low for b in (*left,*right)):
            lows.append(ConfirmedPivot(
                "LOW", i, candidate.low, _utc(candidate.closed_at), confirmed
            ))
    return tuple(highs), tuple(lows)


def _patterns(bars: Sequence[Candle]) -> tuple[str,...]:
    previous, last = bars[-2], bars[-1]
    span = last.high-last.low
    if span == 0:
        return ("ZERO_RANGE",)
    body = abs(last.close-last.open)
    lower = min(last.open,last.close)-last.low
    upper = last.high-max(last.open,last.close)
    patterns = []
    if body / span <= .1:
        patterns.append("DOJI")
    if body / span <= .4 and lower >= .6 * span and lower >= 2 * body and upper <= body:
        patterns.append("HAMMER_SHAPE")
    if body / span <= .4 and upper >= .6 * span and upper >= 2 * body and lower <= body:
        patterns.append("SHOOTING_STAR_SHAPE")
    if (previous.close < previous.open and last.close > last.open
            and last.open <= previous.close and last.close >= previous.open):
        patterns.append("BULLISH_ENGULFING")
    if (previous.close > previous.open and last.close < last.open
            and last.open >= previous.close and last.close <= previous.open):
        patterns.append("BEARISH_ENGULFING")
    if last.high < previous.high and last.low > previous.low:
        patterns.append("INSIDE_BAR")
    if last.high > previous.high and last.low < previous.low:
        patterns.append("OUTSIDE_BAR")
    if body / span >= .9:
        patterns.append("LONG_BODY")
    return tuple(patterns)


def _slope(pivots: tuple[ConfirmedPivot,...], atr_value: float) -> float | None:
    if len(pivots) < 2 or atr_value <= 0:
        return None
    a,b=pivots[-2:]
    separation=b.index-a.index
    return round((b.price-a.price)/separation/atr_value,6) if separation else None


def _chart_patterns(
    high_pivots: tuple[ConfirmedPivot,...],
    low_pivots: tuple[ConfirmedPivot,...],
    atr_value: float, latest_close: float,
) -> tuple[str,...]:
    found=[]
    if len(high_pivots)>=2 and atr_value>0:
        first,second=high_pivots[-2:]
        inside=[p.price for p in low_pivots if first.index<p.index<second.index]
        if inside and abs(first.price-second.price)<=0.5*atr_value:
            neckline=min(inside)
            found.append(
                "DOUBLE_TOP_BREAKDOWN" if latest_close < neckline
                else "POTENTIAL_DOUBLE_TOP"
            )
    if len(low_pivots)>=2 and atr_value>0:
        first,second=low_pivots[-2:]
        inside=[p.price for p in high_pivots if first.index<p.index<second.index]
        if inside and abs(first.price-second.price)<=0.5*atr_value:
            neckline=max(inside)
            found.append(
                "DOUBLE_BOTTOM_BREAKOUT" if latest_close > neckline
                else "POTENTIAL_DOUBLE_BOTTOM"
            )
    return tuple(found)


def analyze_gate_chart(
    bars: Sequence[Candle], *, as_of: datetime,
) -> ProfessionalChartReading:
    """Deterministic context for an analyst; NEVER an entry/exit signal."""
    symbol, timeframe, cutoff = _validated(bars, as_of)
    basic = analyze_closed_candles(bars, as_of=cutoff)
    high_pivots,low_pivots = _confirmed_pivots(bars)
    last=bars[-1]
    support=max((p.price for p in low_pivots if p.price <= last.close), default=None)
    resistance=min((p.price for p in high_pivots if p.price >= last.close), default=None)
    if len(high_pivots)>=2 and len(low_pivots)>=2:
        last_highs=high_pivots[-2:]
        last_lows=low_pivots[-2:]
        if last_highs[-1].price>last_highs[-2].price and last_lows[-1].price>last_lows[-2].price:
            structure="HIGHER_HIGHS_HIGHER_LOWS"
        elif last_highs[-1].price<last_highs[-2].price and last_lows[-1].price<last_lows[-2].price:
            structure="LOWER_HIGHS_LOWER_LOWS"
        else:
            structure="MIXED_STRUCTURE"
    else:
        structure="INSUFFICIENT_CONFIRMED_PIVOTS"
    fib_leg="NONE"
    fib=()
    if high_pivots and low_pivots:
        high,low=high_pivots[-1],low_pivots[-1]
        extent=high.price-low.price
        if extent>0:
            fib_leg="UPSWING" if low.index<high.index else "DOWNSWING"
            ratios=(("0.382",0.382),("0.500",0.5),("0.618",0.618))
            fib=tuple((label,round(
                high.price-extent*ratio if fib_leg=="UPSWING"
                else low.price+extent*ratio,8
            )) for label,ratio in ratios)
    closes=[bar.close for bar in bars]
    macd_result=macd(closes)
    rsi_value=rsi(closes)
    return ProfessionalChartReading(
        exchange=Exchange.GATE.value,symbol=symbol,timeframe=timeframe,
        as_of=cutoff,last_close_at=_utc(last.closed_at),
        candles_analyzed=len(bars),regime=basic.regime,
        ema20=basic.ema20,ema50=basic.ema50,atr14=basic.atr14,
        relative_volume=basic.relative_volume,
        rsi14=round(rsi_value,6) if rsi_value is not None else None,
        macd_histogram=round(macd_result[2],8) if macd_result else None,
        candle_patterns=_patterns(bars),
        chart_patterns=_chart_patterns(high_pivots,low_pivots,basic.atr14,last.close),
        market_structure=structure,
        pivot_highs=high_pivots,
        pivot_lows=low_pivots,
        support=round(support,8) if support is not None else None,
        resistance=round(resistance,8) if resistance is not None else None,
        support_trend_slope_atr_per_bar=_slope(low_pivots,basic.atr14),
        resistance_trend_slope_atr_per_bar=_slope(high_pivots,basic.atr14),
        fibonacci_leg=fib_leg,
        fibonacci_retracements=fib,
        breakout=basic.breakout,
    )


def analyze_gate_multi_timeframe(
    candles_by_timeframe: Mapping[str,Sequence[Candle]], *,
    as_of: datetime,
) -> MultiTimeframeReading:
    """Require fully independently validated 15m/1h/4h closed Gate candles."""
    if set(candles_by_timeframe)!={"15m","1h","4h"}:
        raise ValueError("The Gate multi-timeframe context requires 15m,1h,4h")
    cutoff=_utc(as_of)
    readings=tuple(analyze_gate_chart(candles_by_timeframe[tf],as_of=cutoff)
                   for tf in ("4h","1h","15m"))
    if len({(r.symbol,r.exchange) for r in readings})!=1:
        raise ValueError("Mixed venues or symbols in multi-timeframe context")
    regimes=tuple(r.regime for r in readings)
    if all(x=="UPTREND" for x in regimes):
        alignment="BULLISH_ALIGNMENT"
    elif all(x=="DOWNTREND" for x in regimes):
        alignment="BEARISH_ALIGNMENT"
    else:
        alignment="MIXED_OR_UNCONFIRMED"
    return MultiTimeframeReading(
        exchange=Exchange.GATE.value, symbol=readings[0].symbol,
        as_of=cutoff, alignment=alignment, readings=readings,
    )

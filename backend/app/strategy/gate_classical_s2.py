"""Stage S2.1: versioned classical strategy hypotheses and isolated historical replay.

No exchange client, credentials, OMS, orders, live routing, or agent registration.
The replay assumes hypothetical historical candle availability at the bar close.
That assumption is NOT a historical receipt record or proof of executable alpha.
Never feed the runner its future candles as decision features.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, sqrt
from statistics import fmean, pstdev
from typing import Sequence

from app.schemas.common import Exchange
from app.schemas.market import Candle

TIMEFRAME_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}
ALLOWED_SYMBOLS = frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
MIN_HISTORY = 80


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    description: str
    stop_atr: float
    reward_to_risk: float
    max_hold_bars: int


STRATEGIES = {
    "EMA_TREND_PULLBACK_V1": StrategySpec(
        "EMA_TREND_PULLBACK_V1", "1.0.0", "EMA20/50 trend and closed-candle pullback", 1.5, 2., 24
    ),
    "DONCHIAN_VOLUME_BREAKOUT_V1": StrategySpec(
        "DONCHIAN_VOLUME_BREAKOUT_V1", "1.0.0", "20-bar breakout with volume confirmation", 2., 2., 24
    ),
    "RANGE_BOLLINGER_REVERSION_V1": StrategySpec(
        "RANGE_BOLLINGER_REVERSION_V1", "1.0.0", "20-bar range-extreme mean reversion", 1.5, 1.5, 16
    ),
}


@dataclass(frozen=True)
class ResearchPlan:
    strategy_id: str
    strategy_version: str
    symbol: str
    timeframe: str
    cutoff: datetime
    action: str
    stop_distance: float | None
    target_distance: float | None
    reasoning: str
    execution_authorized: bool = False
    predictive_edge_proven: bool = False


def _ema(values: Sequence[float], period: int) -> float:
    value = fmean(values[:period])
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        value += multiplier * (price - value)
    return value


def _rsi(values: Sequence[float], period: int = 14) -> float:
    differences = [b-a for a,b in zip(values[-period-1:-1],values[-period:])]
    gains = fmean(max(0.,x) for x in differences)
    losses = fmean(max(0.,-x) for x in differences)
    if losses == 0:
        return 100. if gains else 50.
    return 100. - 100. / (1 + gains/losses)


def _atr(bars: Sequence[Candle], period: int = 14) -> float:
    ranges = [
        max(b.high-b.low, abs(b.high-a.close), abs(b.low-a.close))
        for a,b in zip(bars[-period-1:-1], bars[-period:])
    ]
    return fmean(ranges)


def _check_bars(bars: Sequence[Candle], as_of: datetime, *, replay: bool) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Aware decision timestamp required")
    if len(bars) < MIN_HISTORY:
        raise ValueError("At least 80 closed candles required")
    first = bars[0]
    if first.exchange != Exchange.GATE or first.symbol not in ALLOWED_SYMBOLS:
        raise ValueError("Only approved GATE research contracts")
    if first.timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported timeframe")
    period = TIMEFRAME_SECONDS[first.timeframe]
    previous = None
    for b in bars:
        if (b.exchange,b.symbol,b.timeframe)!=(first.exchange,first.symbol,first.timeframe):
            raise ValueError("Mixed candle provenance")
        if b.closed_at.tzinfo is None or b.received_at.tzinfo is None:
            raise ValueError("Naive provenance timestamps")
        if (b.closed_at > as_of or b.received_at < b.closed_at
                or (not replay and b.received_at > as_of)):
            raise ValueError("Future, unclosed, or not-yet-received candle")
        if previous is not None and (b.closed_at - previous).total_seconds()!=period:
            raise ValueError("Gap, duplicate, or nonmonotonic candle history")
        if not all(isfinite(float(v)) for v in (b.open,b.high,b.low,b.close,b.volume)):
            raise ValueError("Nonfinite OHLCV")
        if (min(b.open,b.high,b.low,b.close)<=0 or b.volume<0
                or b.high<max(b.open,b.close,b.low)
                or b.low>min(b.open,b.close,b.high)):
            raise ValueError("Invalid OHLCV")
        previous = b.closed_at
    if (as_of - bars[-1].closed_at).total_seconds() > 2 * period:
        raise ValueError("Stale candle snapshot")


def evaluate_strategy(
    strategy_id: str,
    bars: Sequence[Candle],
    *,
    as_of: datetime,
    historical_replay: bool = False,
) -> ResearchPlan:
    """Compute a hypothesis from only the visible, closed prefix of a series."""
    spec = STRATEGIES[strategy_id]
    _check_bars(bars, as_of, replay=historical_replay)
    symbol, timeframe = bars[0].symbol, bars[0].timeframe
    closes = [b.close for b in bars]
    fast, slow = _ema(closes, 20), _ema(closes, 50)
    prior_fast = _ema(closes[:-1], 20)
    price, previous = closes[-1], closes[-2]
    atr = _atr(bars)
    rsi = _rsi(closes)
    action = "WAIT"
    reason = "Context or confirmation absent"
    if atr > 0 and isfinite(atr):
        if strategy_id == "EMA_TREND_PULLBACK_V1":
            if fast>slow and previous<=prior_fast and price>fast and 45<=rsi<=75:
                action,reason="LONG","Trend-up EMA20 pullback reclaim, RSI confirmed"
            elif fast<slow and previous>=prior_fast and price<fast and 25<=rsi<=55:
                action,reason="SHORT","Trend-down EMA20 pullback rejection, RSI confirmed"
        elif strategy_id == "DONCHIAN_VOLUME_BREAKOUT_V1":
            upper=max(b.high for b in bars[-21:-1])
            lower=min(b.low for b in bars[-21:-1])
            typical=fmean(b.volume for b in bars[-21:-1])
            participation=bars[-1].volume/typical if typical>0 else 0.
            if participation>=1.2 and price>upper and fast>slow:
                action,reason="LONG","20-bar high broken on above-baseline volume"
            elif participation>=1.2 and price<lower and fast<slow:
                action,reason="SHORT","20-bar low broken on above-baseline volume"
        elif strategy_id == "RANGE_BOLLINGER_REVERSION_V1":
            last20=closes[-20:]
            mid=fmean(last20)
            sigma=pstdev(last20)
            # Do not trade structural trends merely because the Bollinger band is touched.
            ranging=abs(fast-slow)/price<.004
            if ranging and sigma>0 and price < mid - 1.7*sigma and rsi<=40:
                action,reason="LONG","Ranging market, below lower Bollinger band and RSI weak"
            elif ranging and sigma>0 and price > mid + 1.7*sigma and rsi>=60:
                action,reason="SHORT","Ranging market, above upper Bollinger band and RSI extended"
    stop=spec.stop_atr*atr if action!="WAIT" else None
    target=spec.reward_to_risk*stop if stop is not None else None
    return ResearchPlan(
        strategy_id=spec.strategy_id,strategy_version=spec.version,
        symbol=symbol,timeframe=timeframe,cutoff=as_of.astimezone(timezone.utc),
        action=action,stop_distance=stop,target_distance=target,
        reasoning=reason,execution_authorized=False,predictive_edge_proven=False,
    )

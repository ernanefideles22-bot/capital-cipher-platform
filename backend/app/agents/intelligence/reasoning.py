"""Deterministic, auditable reasoning scaffold; not a predictive LLM.

This module intentionally cannot place orders or access exchanges. It is a
baseline for comparing future model-assisted hypotheses against known inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timeframe: str
    observed_at: str
    close: float
    ma_fast: float
    ma_medium: float
    ma_slow: float
    recent_high: float
    recent_low: float
    volume: float
    average_volume: float
    source_id: str

    def __post_init__(self) -> None:
        values = (self.close, self.ma_fast, self.ma_medium, self.ma_slow,
                  self.recent_high, self.recent_low, self.volume,
                  self.average_volume)
        if not self.symbol or not self.timeframe or not self.observed_at or not self.source_id:
            raise ValueError("Snapshot provenance and identity are required")
        if not all(isfinite(value) for value in values):
            raise ValueError("Snapshot values must be finite")
        if any(value <= 0 for value in values[:6]):
            raise ValueError("Prices and moving averages must be positive")
        if self.recent_low > self.recent_high:
            raise ValueError("Invalid price range")
        if self.volume < 0 or self.average_volume <= 0:
            raise ValueError("Invalid volume")


@dataclass(frozen=True)
class TradeHypothesis:
    symbol: str
    timeframe: str
    observed_at: str
    source_id: str
    bias: Literal["BULLISH", "BEARISH", "MIXED"]
    action: Literal["WAIT"]
    evidence: tuple[str, ...]
    counter_evidence: tuple[str, ...]
    invalidation: str
    missing_evidence: tuple[str, ...]
    decision_authority: bool = False
    order_authority: bool = False


def analyze_market(snapshot: MarketSnapshot) -> TradeHypothesis:
    """Build an explainable, non-executable market hypothesis.

    Does not estimate win probability, propose a fabricated stop or infer
    missing derivatives/news data. A later evaluation layer must score it.
    """
    bullish = snapshot.ma_fast > snapshot.ma_medium > snapshot.ma_slow
    bearish = snapshot.ma_fast < snapshot.ma_medium < snapshot.ma_slow
    bias: Literal["BULLISH", "BEARISH", "MIXED"] = (
        "BULLISH" if bullish else "BEARISH" if bearish else "MIXED"
    )
    evidence = []
    counter = []
    if bullish:
        evidence.append("Fast, medium and slow moving averages are bullishly aligned")
    elif bearish:
        evidence.append("Fast, medium and slow moving averages are bearishly aligned")
    else:
        counter.append("Moving averages do not show clean directional alignment")
    if snapshot.close > snapshot.ma_fast:
        evidence.append("Last close is above the fast moving average")
    elif snapshot.close < snapshot.ma_fast:
        counter.append("Last close is below the fast moving average")
    else:
        counter.append("Last close equals the fast moving average")
    relative_volume = snapshot.volume / snapshot.average_volume
    if relative_volume < 0.8:
        counter.append("Participation is below the supplied volume baseline")
    elif relative_volume > 1.2:
        evidence.append("Participation is above the supplied volume baseline")
    else:
        evidence.append("Participation is near the supplied volume baseline")
    if snapshot.close > snapshot.recent_high:
        evidence.append("Last close is above the supplied prior range high")
    elif snapshot.close < snapshot.recent_low:
        evidence.append("Last close is below the supplied prior range low")
    else:
        counter.append("Last close remains inside the supplied prior range")
    invalidation = (
        f"Reassess bullish hypothesis if price closes below {snapshot.recent_low:g}"
        if bullish else
        f"Reassess bearish hypothesis if price closes above {snapshot.recent_high:g}"
        if bearish else
        "No directional hypothesis to invalidate; await a clearer regime"
    )
    return TradeHypothesis(
        symbol=snapshot.symbol, timeframe=snapshot.timeframe,
        observed_at=snapshot.observed_at, source_id=snapshot.source_id,
        bias=bias, action="WAIT", evidence=tuple(evidence),
        counter_evidence=tuple(counter), invalidation=invalidation,
        missing_evidence=("order_book", "open_interest", "funding",
                          "liquidations", "verified_news", "higher_timeframes"),
    )

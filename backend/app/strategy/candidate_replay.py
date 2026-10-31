"""Deterministic, offline replay of an agent's OWN bounded candle strategy.

No OMS/PAPER/exchange imports; no network, live feed or order side effects.
This is an experimental one-symbol, one-book simulator, NOT a release gate.
Signal: fully closed and received candle; entry: earliest subsequent candle
OPEN at/after the signal is available; stop/target: intrabar stop-first.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.schemas.market import Candle
from app.strategy.market_intelligence import analyze_closed_candles
from app.strategy.research_lab import StrategyHypothesis

SUPPORTED_FEATURES = frozenset({
    "trend_up", "trend_down", "regime_range", "regime_high_volatility",
    "pullback_ema20", "breakout_up", "breakout_down", "volume_confirmation",
    "structure_higher_lows", "structure_lower_highs", "support_rejection",
    "resistance_rejection", "momentum_positive", "momentum_negative",
    "rsi_oversold", "rsi_overbought",
})
BAR_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400}


@dataclass(frozen=True)
class CandidateTrade:
    entry_at: datetime
    exit_at: datetime
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    stop_price: float
    target_price: float
    exit_reason: str
    gross_pnl: float
    execution_costs: float
    net_pnl: float


@dataclass(frozen=True)
class CandidateReplayReport:
    fingerprint: str
    dataset_hash: str
    symbol: str
    timeframe: str
    bars_processed: int
    trades: tuple[CandidateTrade, ...]
    net_pnl: float
    net_return_percent: float
    total_costs: float
    max_drawdown_percent: float
    skipped_stale_entries: int
    all_in_cost_bps_per_side: float
    venue_policy: str = "SINGLE_OWNER_ONE_WAY"
    status: str = "RESEARCH_ONLY"
    execution_authorized: bool = False


class OneWayExposureGuard:
    """Conservative fail-closed, single position owner per symbol.

    Opposite or same-direction concurrent books on the same symbol are
    refused, instead of pretending one-way exchange netting is two hedges.
    Separate guard per simulation portfolio; NEVER a broker risk control.
    """

    def __init__(self) -> None:
        self._owners: dict[str, tuple[str, str]] = {}

    def reserve(self, symbol: str, side: str, owner: str) -> None:
        if symbol in self._owners:
            raise ValueError("One-way netting: concurrent symbol exposure refused")
        if side not in ("BUY", "SELL") or not owner:
            raise ValueError("Invalid position identity")
        self._owners[symbol] = (side, owner)

    def release(self, symbol: str, owner: str) -> None:
        if symbol not in self._owners or self._owners[symbol][1] != owner:
            raise ValueError("One-way netting: release identity mismatch")
        del self._owners[symbol]

    def is_reserved(self, symbol: str) -> bool:
        return symbol in self._owners


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware market timestamps required")
    return value.astimezone(timezone.utc)


def _validate_bars(hypothesis: StrategyHypothesis, bars: Sequence[Candle]) -> str:
    if len(bars) < 80:
        raise ValueError("At least 80 closed candles required")
    step = timedelta(seconds=BAR_SECONDS[hypothesis.timeframe])
    previous_close = previous_receipt = None
    digest = hashlib.sha256()
    for bar in bars:
        if bar.symbol != hypothesis.symbol or bar.timeframe != hypothesis.timeframe:
            raise ValueError("Candles do not match hypothesis universe")
        if bar.exchange != bars[0].exchange:
            raise ValueError("Mixed exchanges in historical dataset")
        close, received = _utc(bar.closed_at), _utc(bar.received_at)
        if received < close:
            raise ValueError("Candle available before it closed")
        if previous_close is not None:
            if close - previous_close != step:
                raise ValueError("Missing, duplicate or irregular OHLCV bars")
            if received <= previous_receipt:
                raise ValueError("Non-monotonic candle receipt timestamps")
        if not all(math.isfinite(v) for v in (
            bar.open, bar.high, bar.low, bar.close, bar.volume
        )):
            raise ValueError("Non-finite market price/volume")
        payload = bar.model_dump(mode="json")
        digest.update(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode())
        digest.update(b"\n")
        previous_close, previous_receipt = close, received
    return digest.hexdigest()


def _rsi14(bars: Sequence[Candle]) -> float:
    closes = [b.close for b in bars[-15:]]
    gains = sum(max(closes[i] - closes[i-1], 0) for i in range(1, 15))
    losses = sum(max(closes[i-1] - closes[i], 0) for i in range(1, 15))
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def _matched(
    feature: str, hypothesis: StrategyHypothesis, bars: Sequence[Candle],
    snap,
) -> bool:
    last = bars[-1]
    params = dict(hypothesis.parameters)
    side = hypothesis.side
    if feature == "trend_up":
        return snap.regime == "UPTREND"
    if feature == "trend_down":
        return snap.regime == "DOWNTREND"
    if feature == "regime_range":
        return snap.regime == "RANGE_OR_TRANSITION"
    if feature == "regime_high_volatility":
        return snap.regime == "HIGH_VOLATILITY"
    if feature == "pullback_ema20":
        return (last.low <= snap.ema20 < last.close if side == "BUY"
                else last.high >= snap.ema20 > last.close)
    if feature == "breakout_up":
        return snap.breakout == "UP"
    if feature == "breakout_down":
        return snap.breakout == "DOWN"
    if feature == "volume_confirmation":
        return snap.relative_volume >= params.get("volume_ratio_min", 1.2)
    if feature == "structure_higher_lows":
        return bars[-3].low < bars[-2].low < last.low
    if feature == "structure_lower_highs":
        return bars[-3].high > bars[-2].high > last.high
    if feature == "support_rejection":
        return snap.candle_pattern == "LOWER_WICK_REJECTION"
    if feature == "resistance_rejection":
        return snap.candle_pattern == "UPPER_WICK_REJECTION"
    if feature == "momentum_positive":
        return last.close > bars[-5].close
    if feature == "momentum_negative":
        return last.close < bars[-5].close
    if feature == "rsi_oversold":
        return _rsi14(bars) < 30
    if feature == "rsi_overbought":
        return _rsi14(bars) > 70
    raise ValueError("Unimplemented research predicate: " + feature)


def replay_candidate(
    hypothesis: StrategyHypothesis,
    candles: Sequence[Candle],
    *,
    initial_equity: float = 10_000.0,
    all_in_cost_bps_per_side: float = 12.0,
    risk_fraction: float = 0.005,
    evaluation_start: int = 60,
) -> CandidateReplayReport:
    """Replay original agent predicates; never execute an actual trade.

    Historic OHLCV has no intrabar tick sequence: when both brackets touch
    in one candle, assume STOP first; adverse stop gaps fill at open.
    Candidate observations only start after evaluation_start, but earlier
    bars may be used for as-of lookback.
    """
    if not (math.isfinite(initial_equity) and initial_equity > 0):
        raise ValueError("Invalid initial equity")
    if not (math.isfinite(all_in_cost_bps_per_side)
            and 12 <= all_in_cost_bps_per_side <= 1000):
        raise ValueError("Cost stress must be >=12 and <=1000 bps per side")
    if not (math.isfinite(risk_fraction) and 0 < risk_fraction <= 0.01):
        raise ValueError("Invalid per-trade risk fraction")
    unknown = (set(hypothesis.entry_features) | {hypothesis.invalidation_feature}
               ) - SUPPORTED_FEATURES
    if unknown:
        raise ValueError("Unsupported research predicates: " + ", ".join(sorted(unknown)))
    dataset_hash = _validate_bars(hypothesis, candles)
    if not 60 <= evaluation_start < len(candles) - 1:
        raise ValueError("Invalid OOS start boundary")
    step = timedelta(seconds=BAR_SECONDS[hypothesis.timeframe])
    fee = all_in_cost_bps_per_side / 10_000
    owner = hypothesis.fingerprint
    guard = OneWayExposureGuard()
    equity = peak = initial_equity
    max_dd = 0.0
    pending_entry: tuple[datetime, float, int] | None = None
    pending_exit: datetime | None = None
    position: tuple[datetime, float, float, float, float, float] | None = None
    # position: entry_at, price, qty, stop, target, entry_cost
    trades: list[CandidateTrade] = []
    skipped = 0
    last_exit_idx = -10_000
    params = dict(hypothesis.parameters)

    def close_position(index: int, price: float, reason: str, at: datetime) -> None:
        nonlocal position, equity, peak, max_dd, last_exit_idx
        assert position is not None
        entered, entry, qty, stop, target, entry_cost = position
        signed = 1 if hypothesis.side == "BUY" else -1
        gross = signed * (price - entry) * qty
        costs = entry_cost + price * qty * fee
        net = gross - costs
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, 100 * (peak - equity) / peak)
        trades.append(CandidateTrade(
            entered, at, hypothesis.side, entry, price, qty, stop, target,
            reason, gross, costs, net,
        ))
        guard.release(hypothesis.symbol, owner)
        position = None
        last_exit_idx = index

    for i, bar in enumerate(candles):
        opened = _utc(bar.closed_at) - step
        closed = _utc(bar.closed_at)
        # Pending exit triggered ONLY by a preceding completed candle.
        if position is not None and pending_exit is not None and opened >= pending_exit:
            close_position(i, bar.open, "RULE_INVALIDATED", opened)
            pending_exit = None

        # Queue entry only after an as-of close; when its receipt is later
        # than the next open, wait for the first later executable open.
        if position is None and pending_entry is not None:
            available, atr, signal_i = pending_entry
            if i - signal_i > 4:
                skipped += 1
                pending_entry = None
            elif opened >= available and i < len(candles) - 1:
                risk_distance = atr * params.get("atr_stop_multiple", 1.5)
                if risk_distance > 0 and equity > 0:
                    price = bar.open
                    direction = 1 if hypothesis.side == "BUY" else -1
                    stop = price - direction * risk_distance
                    target = price + direction * risk_distance * params.get("target_r_multiple", 2.0)
                    if stop > 0 and target > 0:
                        qty = min(equity * risk_fraction / risk_distance, equity / (price * (1 + fee)))
                        if qty > 0:
                            guard.reserve(hypothesis.symbol, hypothesis.side, owner)
                            position = (opened, price, qty, stop, target, price * qty * fee)
                pending_entry = None

        if position is not None:
            _, _, _, stop, target, _ = position
            if hypothesis.side == "BUY":
                stop_hit = bar.low <= stop
                target_hit = bar.high >= target
                stop_fill = min(bar.open, stop)  # adverse gap
                target_fill = target  # no favorable gap improvement
            else:
                stop_hit = bar.high >= stop
                target_hit = bar.low <= target
                stop_fill = max(bar.open, stop)
                target_fill = target
            if stop_hit:
                close_position(i, stop_fill, "STOP_LOSS", closed)
                pending_exit = None
            elif target_hit:
                close_position(i, target_fill, "TAKE_PROFIT", closed)
                pending_exit = None

        if i < evaluation_start or i >= len(candles) - 1:
            continue
        # A data-source receipt must be ordered; all features are derived
        # ONLY from bars completed and received up through this event.
        history = candles[:i + 1]
        snap = analyze_closed_candles(history[-60:], as_of=_utc(bar.received_at))
        if position is not None:
            if _matched(hypothesis.invalidation_feature, hypothesis, history, snap):
                pending_exit = _utc(bar.received_at)
        elif (pending_entry is None
              and i - last_exit_idx > int(params.get("cooldown_bars", 0))
              and all(_matched(f, hypothesis, history, snap)
                      for f in hypothesis.entry_features)):
            pending_entry = (_utc(bar.received_at), snap.atr14, i)

    if position is not None:
        final = candles[-1]
        close_position(len(candles) - 1, final.close, "REPLAY_END", _utc(final.received_at))
    return CandidateReplayReport(
        fingerprint=hypothesis.fingerprint,
        dataset_hash=dataset_hash,
        symbol=hypothesis.symbol,
        timeframe=hypothesis.timeframe,
        bars_processed=len(candles),
        trades=tuple(trades),
        net_pnl=round(equity - initial_equity, 8),
        net_return_percent=round(100 * (equity / initial_equity - 1), 8),
        total_costs=round(sum(x.execution_costs for x in trades), 8),
        max_drawdown_percent=round(max_dd, 8),
        skipped_stale_entries=skipped,
        all_in_cost_bps_per_side=all_in_cost_bps_per_side,
    )

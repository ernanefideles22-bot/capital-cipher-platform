"""E2.1 matched signal replay: one frozen fill, exposure and cost model.

This is a research overlay for the CURRENT 3-core-agent BacktestingEngine
observations and a previously frozen chart hypothesis. It does NOT represent
all 300 runtime agents, deployed PAPER fills or executable exchange quotes.
No OMS/broker/database writes. Historical E2.0 dataset has ALREADY been viewed
and is therefore NOT an untouched confirmatory holdout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Mapping, Sequence

from app.schemas.market import Candle
from app.strategy.candidate_replay import (
    BAR_SECONDS, _matched, _utc, _validate_bars,
)
from app.strategy.market_intelligence import analyze_closed_candles
from app.strategy.research_lab import StrategyHypothesis

INITIAL_EQUITY = 10_000.0
RISK_FRACTION = 0.005
ATR_STOP_MULTIPLE = 1.5
TARGET_R = 2.0
STALE_SIGNAL_BARS = 4
COOLDOWN_BARS = 2


@dataclass(frozen=True)
class SignalEvent:
    candle_index: int
    available_at: datetime
    side: str
    atr14: float

    def __post_init__(self):
        if self.candle_index < 60 or self.side not in {"BUY", "SELL"}:
            raise ValueError("Invalid timed research signal")
        if not isfinite(self.atr14) or self.atr14 <= 0:
            raise ValueError("Invalid ATR in signal")
        _utc(self.available_at)


def candidate_signal_events(
    hypothesis: StrategyHypothesis, bars: Sequence[Candle],
    *, evaluation_start: int,
) -> list[SignalEvent]:
    """Frozen candidate entry predicates, independent of position/execution."""
    _validate_bars(hypothesis, bars)
    if evaluation_start < 60 or evaluation_start >= len(bars) - 1:
        raise ValueError("Invalid research boundary")
    results = []
    for i in range(evaluation_start, len(bars) - 1):
        view = bars[:i + 1]
        snap = analyze_closed_candles(view[-60:], as_of=_utc(bars[i].received_at))
        if all(_matched(f, hypothesis, view, snap) for f in hypothesis.entry_features):
            if snap.atr14 > 0:
                results.append(SignalEvent(
                    i, _utc(bars[i].received_at), hypothesis.side, snap.atr14
                ))
    return results


def native_signal_events(
    native_decisions: Sequence[tuple[int, str, str]],
    bars: Sequence[Candle], *, evaluation_start: int,
    only_risk_approved: bool = False,
) -> list[SignalEvent]:
    """Records from the unmodified three-core-agent decision pipeline.

    Native decision actions are returned after risk validation; retain both
    pre-approval candidate signal and a separately labeled approved subset.
    An approved subset is path-dependent on the native engine's simulated
    positions and MUST NOT be treated as risk controls for the common replay.
    """
    if evaluation_start < 60 or evaluation_start >= len(bars) - 1:
        raise ValueError("Invalid research boundary")
    indices = set()
    output = []
    for i, action, risk in native_decisions:
        if not evaluation_start <= i < len(bars) - 1:
            continue
        if i in indices:
            raise ValueError("Duplicate native decision for same candle")
        indices.add(i)
        if action not in {"BUY", "SELL"}:
            continue
        if only_risk_approved and risk not in {"APPROVED", "REDUCED"}:
            continue
        view = bars[:i + 1]
        snap = analyze_closed_candles(view[-60:], as_of=_utc(bars[i].received_at))
        if snap.atr14 > 0:
            output.append(SignalEvent(i, _utc(bars[i].received_at), action, snap.atr14))
    return output


def matched_replay(
    bars: Sequence[Candle], events: Sequence[SignalEvent], *,
    evaluation_start: int, cost_bps_per_side: float,
) -> dict:
    """Replay all arms with identical entry/exit rules and fictional balances.

    Entry at first subsequent OPEN not earlier than signal received_at.
    Stop-first when stop and target hit same OHLCV candle. Adverse gaps use
    bar OPEN at stop; target fills no better than planned target. Fixed costs
    per side are applied ONCE at entry and exit. Only one position per symbol.
    Candidate invalidation exits are excluded in ALL arms deliberately.
    """
    if not 12 <= cost_bps_per_side <= 1000 or not isfinite(cost_bps_per_side):
        raise ValueError("Uncalibrated/invalid scenario costs")
    if not 60 <= evaluation_start < len(bars) - 1:
        raise ValueError("Invalid holdout boundary")
    if not bars:
        raise ValueError("No candles")
    step = timedelta(seconds=BAR_SECONDS[bars[0].timeframe])
    fee = cost_bps_per_side / 10000.0
    source = {}
    for event in events:
        if event.candle_index in source:
            raise ValueError("Only one signal per candle")
        if not evaluation_start <= event.candle_index < len(bars) - 1:
            raise ValueError("Signal outside evaluation")
        if _utc(event.available_at) < _utc(bars[event.candle_index].received_at):
            raise ValueError("Signal before market receipt/lookahead")
        source[event.candle_index] = event

    equity = peak = INITIAL_EQUITY
    max_dd = 0.0
    pending: SignalEvent | None = None
    position = None
    # Position tuple: (side, entry_price, quantity, stop, target, entry_cost)
    trades = []
    last_exit = -100000
    skipped = 0
    entries = 0

    def exit_at(price: float, reason: str, at: datetime):
        nonlocal position, equity, peak, max_dd, last_exit
        assert position is not None
        side, entry, qty, stop, target, entry_cost = position
        sign = 1.0 if side == "BUY" else -1.0
        gross = sign * (price - entry) * qty
        fees = entry_cost + price * qty * fee
        net = gross - fees
        if not all(isfinite(x) for x in (net, fees, gross)):
            raise ValueError("Invalid trade result")
        trades.append({
            "net_pnl": net, "exit_reason": reason, "side": side,
            "exit_at": at.isoformat(), "fees": fees,
        })
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
        position = None

    for i, bar in enumerate(bars):
        if i < evaluation_start:
            continue
        opened = _utc(bar.closed_at) - step
        closed = _utc(bar.closed_at)
        exited = False

        # Open pending position only after the SIGNAL ARRIVED to the system.
        if position is None and pending is not None:
            if i - pending.candle_index > STALE_SIGNAL_BARS:
                skipped += 1
                pending = None
            elif i > pending.candle_index and opened >= _utc(pending.available_at):
                distance = pending.atr14 * ATR_STOP_MULTIPLE
                sign = 1.0 if pending.side == "BUY" else -1.0
                price = bar.open
                stop = price - sign * distance
                target = price + sign * distance * TARGET_R
                if stop > 0 and target > 0 and equity > 0:
                    qty = min(
                        equity * RISK_FRACTION / distance,
                        equity / (price * (1 + fee))
                    )
                    if qty > 0:
                        position = (
                            pending.side, price, qty, stop, target,
                            price * qty * fee,
                        )
                        entries += 1
                pending = None

        # Same-bar bracket execution is intentionally adverse and symmetrical.
        if position is not None:
            side, entry, qty, stop, target, entry_cost = position
            if side == "BUY":
                stop_hit, target_hit = bar.low <= stop, bar.high >= target
                stop_fill = min(bar.open, stop)
            else:
                stop_hit, target_hit = bar.high >= stop, bar.low <= target
                stop_fill = max(bar.open, stop)
            if stop_hit:
                exit_at(stop_fill, "STOP_LOSS", closed)
                exited = True
                last_exit = i
            elif target_hit:
                exit_at(target, "TAKE_PROFIT", closed)
                exited = True
                last_exit = i

        # Do not reopen immediately after a stop in the same bar. No new
        # candidate may enter during a position or cooldown.
        if (position is None and pending is None and not exited
                and i < len(bars) - 1 and i - last_exit > COOLDOWN_BARS
                and i in source):
            pending = source[i]

    if position is not None:
        exit_at(bars[-1].close, "REPLAY_END", _utc(bars[-1].received_at))

    profits = [x["net_pnl"] for x in trades if x["net_pnl"] > 0]
    losses = [-x["net_pnl"] for x in trades if x["net_pnl"] < 0]
    count = len(trades)
    return {
        "signals": len(events),
        "entered": entries,
        "closed_including_replay_end": count,
        "wins": len(profits),
        "win_rate": round(len(profits) / count, 6) if count else None,
        "profit_factor": round(sum(profits) / sum(losses), 6) if losses else None,
        "profit_factor_status": "NO_LOSSES_UNDEFINED" if not losses else "FINITE",
        "net_pnl": round(equity - INITIAL_EQUITY, 8),
        "net_return_percent": round(100 * (equity / INITIAL_EQUITY - 1), 8),
        "max_drawdown_percent": round(max_dd, 8),
        "net_expectancy_per_close": round((equity - INITIAL_EQUITY) / count, 8)
                                      if count else None,
        "total_execution_fees": round(sum(x["fees"] for x in trades), 8),
        "stop_losses": sum(t["exit_reason"] == "STOP_LOSS" for t in trades),
        "take_profits": sum(t["exit_reason"] == "TAKE_PROFIT" for t in trades),
        "skipped_stale": skipped,
        "cost_bps_per_side": cost_bps_per_side,
        "execution_authorized": False,
        "common_model_id": "E21_NEXT_AVAILABLE_OPEN_STOP_FIRST_R2_V1",
        "bar_count": len(bars),
    }

"""Isolated slow-momentum replay; legacy playbook replay and journals unchanged."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import timedelta, timezone
from math import ceil, isfinite

from app.strategy.gate_classical_s2 import _check_bars
from app.strategy.gate_s2_replay import (
    Friction, ResearchRisk, Trade, _OpenPosition, _adverse_execution, _exit_trigger, _summary,
)
from app.strategy.gate_s4_complete_playbooks import PlaybookDecision
from app.strategy.gate_s4_slow_momentum import POLICY, POLICY_ID, SYMBOLS, planning_cost_fraction


@dataclass
class _Position:
    bracket: _OpenPosition
    decision: PlaybookDecision


def replay_momentum(bars, reader, *, start, end, segment,
                    friction=Friction(), risk=ResearchRisk(), extra_delay=0):
    policy_id = POLICY_ID
    if (type(extra_delay) is not int or extra_delay not in (0, 1)
            or not 220 <= start < end <= len(bars)
            or bars[0].timeframe != "4h" or bars[0].symbol not in SYMBOLS):
        raise ValueError("Unsupported playbook replay request")
    _check_bars(bars[:end], bars[end - 1].closed_at + timedelta(microseconds=1), replay=True)
    policy = POLICY
    equity = peak = day_start = risk.initial_equity
    max_dd, blocked, day, position, cooldown_until = 0., 0, None, None, -1
    trades, skips, consumed = [], Counter(), set()
    clock_checks = 0

    def close(index, raw, reason):
        nonlocal equity, peak, max_dd, position, cooldown_until
        p = position.bracket
        exit_price = _adverse_execution(raw, p.direction, opening=False, friction=friction)
        gross = (exit_price - p.entry) * p.quantity * p.direction
        fees = (p.entry + exit_price) * p.quantity * friction.fee_bps_side / 10000
        elapsed = (bars[index].closed_at - bars[p.entered_index].closed_at).total_seconds()
        funding = p.entry * p.quantity * friction.funding_bps_8h / 10000 * max(1, ceil(max(0., elapsed) / 28800))
        net = gross - fees - funding
        trades.append(Trade(policy_id, p.side, p.decision_index, p.entered_index, index,
                            p.entry, exit_price, p.quantity, p.stop, p.target, gross, fees, funding, net, reason))
        equity += net
        peak = max(peak, equity)
        max_dd = max(max_dd, (1 - equity / peak) * 100)
        cooldown_until = index + policy.cooldown_bars + 1
        position = None

    for j in range(start + 2 + extra_delay, end):
        bar = bars[j]
        current_day = bar.closed_at.astimezone(timezone.utc).date()
        if current_day != day:
            day, day_start = current_day, equity
        if position is not None:
            p, d = position.bracket, position.decision
            # Existing brackets have priority when the market gaps beyond them.
            trigger = _exit_trigger(bar, p)
            gap = trigger is not None and trigger[1] in ("STOP_GAP", "TARGET_GAP_LIMIT")
            invalid_bar_index = j - 2  # Closed invalidation known strictly before this OPEN.
            thesis_broken = (invalid_bar_index >= p.entered_index and
                p.direction * reader.momentum(invalid_bar_index + 1) <= 0)
            if gap:
                close(j, *trigger)
            elif j - p.entered_index >= policy.max_hold_bars:
                close(j, bar.open, "TIME_LIMIT_NEXT_OPEN")
            elif thesis_broken:
                close(j, bar.open, "THESIS_INVALIDATION_NEXT_OPEN")
            elif trigger is not None:
                close(j, *trigger)
            if position is not None:
                continue
            continue  # No same-bar reentry after any exit.
        if j < cooldown_until:
            skips["COOLDOWN"] += 1
            continue
        if (equity <= 0 or equity < day_start * (1 - risk.max_daily_loss_pct / 100)
                or equity < peak * (1 - risk.max_drawdown_pct / 100)):
            blocked += 1
            continue
        decision_index = j - 2 - extra_delay
        clock = bars[decision_index].closed_at + timedelta(microseconds=1)
        d = reader.decision(policy_id, decision_index + 1, clock)
        if (not isinstance(d, PlaybookDecision) or d.policy_id != policy_id or d.version != policy.version
                or d.symbol != bars[0].symbol or d.timeframe != bars[0].timeframe
                or d.available_at != clock or d.execution_authorized is not False
                or d.predictive_edge_proven is not False or d.action not in ("WAIT", "LONG", "SHORT")):
            raise ValueError("Untrusted playbook decision")
        if d.action == "WAIT":
            continue
        if (not d.setup_id or d.setup_index is None or d.setup_index > decision_index
                or not all(v is not None and isfinite(v) for v in
                           (d.reference_price, d.atr, d.stop_price, d.target_price, d.invalidation_level))
                or min(d.reference_price, d.atr, d.stop_price, d.target_price) <= 0):
            raise ValueError("Invalid structural playbook levels")
        if d.setup_id in consumed:
            skips["SETUP_ALREADY_CONSUMED"] += 1
            continue
        consumed.add(d.setup_id)  # No retry until an attractive historical fill appears.
        if j - d.setup_index > policy.setup_expiry_bars:
            skips["SETUP_EXPIRED"] += 1
            continue
        if bars[j - 1].closed_at <= d.available_at:
            raise ValueError("Entry OPEN is not strictly after decision availability")
        direction = 1 if d.action == "LONG" else -1
        intervening = bars[decision_index + 1:j]
        if any((b.low <= d.stop_price if direction == 1 else b.high >= d.stop_price) for b in intervening):
            skips["INVALIDATED_BEFORE_ENTRY"] += 1
            continue
        if any((b.high >= d.target_price if direction == 1 else b.low <= d.target_price) for b in intervening):
            skips["TARGET_ALREADY_TOUCHED_BEFORE_ENTRY"] += 1
            continue
        entry = _adverse_execution(bar.open, direction, opening=True, friction=friction)
        if abs(entry - d.reference_price) > .5 * d.atr:
            skips["ENTRY_MOVED_TOO_FAR"] += 1
            continue
        distance = direction * (entry - d.stop_price)
        if not .5 * d.atr <= distance <= 3 * d.atr:
            skips["ENTRY_RISK_GEOMETRY"] += 1
            continue
        target = entry + direction * policy.reward_to_risk * distance
        if direction * (target - entry) / distance < policy.reward_to_risk - 1e-10:
            skips["ENTRY_REWARD_TOO_SMALL"] += 1
            continue
        if min(d.stop_price, target) <= 0 or planning_cost_fraction() > .20 * abs(target - entry) / entry:
            skips["EXECUTED_ENTRY_COST_BUDGET"] += 1
            continue
        quantity = min(equity * risk.risk_per_trade_pct / 100 / distance,
                       equity * risk.max_notional_pct / 100 / entry)
        if not isfinite(quantity) or quantity <= 0:
            continue
        position = _Position(_OpenPosition(policy_id, d.action, direction, decision_index, j,
                                           entry, quantity, d.stop_price, target), d)
        clock_checks += 1
        trigger = _exit_trigger(bar, position.bracket)
        if trigger is not None:
            close(j, *trigger)
    if position is not None:
        close(end - 1, bars[end - 1].close, "END_WINDOW_ESTIMATE")
    report = _summary(policy_id, segment, end - start, trades, risk.initial_equity, equity, blocked, max_dd)
    return {"report": report, "skip_counts": dict(skips), "distinct_setups_attempted": len(consumed),
            "strict_entry_clock_checks": clock_checks, "execution_authorized": False,
            "original_risk_policy_modified": False}

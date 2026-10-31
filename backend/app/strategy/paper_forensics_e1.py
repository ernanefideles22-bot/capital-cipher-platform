"""E1: deterministic PAPER loss forensics, not a trading signal.

Input is a read-only snapshot of paper_orders plus matching decisions, risk,
audit closures and observational agent outputs. Flags are diagnostics, NOT
proven causal explanations. No change to OMS, risk, broker or agent weights.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Iterable


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Audit timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AgentObservation:
    agent: str
    signal: str
    observed_at: datetime
    confidence: int | None = None

    def __post_init__(self):
        if not self.agent or self.signal not in {
            "BUY", "SELL", "HOLD", "WAIT", "BLOCK", "NEUTRAL"
        }:
            raise ValueError("Invalid observational agent output")
        _utc(self.observed_at)
        if self.confidence is not None and not 0 <= self.confidence <= 100:
            raise ValueError("Agent confidence must be 0..100")


@dataclass(frozen=True)
class PaperTrace:
    order_id: str
    decision_id: str
    risk_check_id: str
    correlation_id: str
    symbol: str
    timeframe: str
    strategy: str
    side: str
    status: str
    entry_price: float
    position_notional: float
    opened_at: datetime
    closed_at: datetime | None
    pnl_net: float | None
    stop_loss: float | None = None
    take_profit: float | None = None
    fees_estimated: float | None = None
    slippage_estimated: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    decision_action: str | None = None
    decision_confidence: int | None = None
    decision_trace_present: bool = False
    risk_trace_present: bool = False
    risk_approved: bool | None = None
    risk_reward_declared: float | None = None
    audit_close_present: bool = False
    agents: tuple[AgentObservation, ...] = ()
    exchange: str = "UNKNOWN"
    # Processing clock for evidence sequencing. opened_at is market candle
    # time and may predate a decision produced after the candle closed.
    order_created_at: datetime | None = None

    def __post_init__(self):
        if not all((self.order_id, self.decision_id, self.risk_check_id,
                    self.correlation_id, self.symbol, self.strategy)):
            raise ValueError("Required source trace IDs must be present")
        if not self.exchange:
            raise ValueError("Missing exchange identity")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("Invalid direction")
        if self.status not in {"CLOSED", "FILLED"}:
            raise ValueError("E1 only supports closed and open filled PAPER")
        opened = _utc(self.opened_at)
        processing = _utc(self.order_created_at) or opened
        closed = _utc(self.closed_at)
        if closed is not None and closed < opened:
            raise ValueError("Close precedes open")
        if self.status == "CLOSED" and (closed is None or self.pnl_net is None):
            raise ValueError("Closed order missing net PnL or close time")
        if self.status == "FILLED" and (closed is not None or self.pnl_net is not None):
            raise ValueError("Open order cannot claim realized PnL")
        amounts = (self.entry_price, self.position_notional,
                   self.pnl_net, self.stop_loss, self.take_profit,
                   self.fees_estimated, self.slippage_estimated, self.exit_price,
                   self.risk_reward_declared)
        if any(v is not None and not isfinite(v) for v in amounts):
            raise ValueError("Nonfinite monetary or price field")
        if self.entry_price <= 0 or self.position_notional <= 0:
            raise ValueError("Invalid positive position notional / entry price")
        if self.fees_estimated is not None and self.fees_estimated < 0:
            raise ValueError("Negative fees")
        if self.slippage_estimated is not None and self.slippage_estimated < 0:
            raise ValueError("Negative slippage estimate")
        if self.decision_confidence is not None and not 0 <= self.decision_confidence <= 100:
            raise ValueError("Invalid decision confidence")
        for o in self.agents:
            if _utc(o.observed_at) > processing:
                raise ValueError("Post-creation agent output cannot explain an entry")


@dataclass(frozen=True)
class TradeFinding:
    order_id: str
    strategy: str
    symbol: str
    timeframe: str
    side: str
    realized_net: float | None
    realized_r: float | None
    planned_risk_notional: float | None
    planned_reward_risk: float | None
    flags: tuple[str, ...]
    evidence_status: str
    agent_agreement: str
    source_attribution: str = "OBSERVATIONAL_ONLY_NOT_CAUSAL"


@dataclass(frozen=True)
class GroupScore:
    key: str
    trades: int
    wins: int
    net_pnl: float
    average_net: float | None
    profit_factor: float | None
    profit_factor_status: str
    enough_for_ranking: bool


@dataclass(frozen=True)
class ForensicsReport:
    closed_trades: int
    open_trades: int
    net_pnl: float
    wins: int
    win_rate: float | None
    profit_factor: float | None
    profit_factor_status: str
    missing_audit_closures: int
    missing_decision_traces: int
    missing_risk_traces: int
    agent_attribution_verified: bool
    flags_count: dict[str, int]
    by_strategy: tuple[GroupScore, ...]
    by_symbol: tuple[GroupScore, ...]
    by_timeframe: tuple[GroupScore, ...]
    by_exit_reason: tuple[GroupScore, ...]
    by_side: tuple[GroupScore, ...]
    trades: tuple[TradeFinding, ...]
    limitations: tuple[str, ...]
    mode: str = "PAPER_READ_ONLY_E1"
    execution_authorized: bool = False


def _group(items: Iterable[PaperTrace], attr: str, minimum_samples: int) -> tuple[GroupScore, ...]:
    grouped: dict[str, list[PaperTrace]] = defaultdict(list)
    for item in items:
        grouped[str(getattr(item, attr) or "UNKNOWN")].append(item)
    result = []
    for key, rows in grouped.items():
        profits = [float(row.pnl_net) for row in rows]
        wins = sum(x > 0 for x in profits)
        positive = sum(x for x in profits if x > 0)
        losses = -sum(x for x in profits if x < 0)
        pf = positive / losses if losses else None
        status = (
            "NO_LOSSES_UNDEFINED" if losses == 0
            else "ZERO_PROFIT" if positive == 0
            else "FINITE"
        )
        result.append(GroupScore(
            key=key, trades=len(profits), wins=wins,
            net_pnl=round(sum(profits), 6),
            average_net=round(sum(profits) / len(profits), 6),
            profit_factor=round(pf, 6) if pf is not None else None,
            profit_factor_status=status,
            enough_for_ranking=len(profits) >= minimum_samples,
        ))
    return tuple(sorted(result, key=lambda g: g.key))


def _agents_vote(t: PaperTrace) -> str:
    votes = {x.signal for x in t.agents if x.signal in {"BUY", "SELL"}}
    if not votes:
        return "NO_DIRECTIONAL_EVIDENCE"
    if len(votes) > 1:
        return "CONFLICTING_AGENT_OPINIONS"
    if t.side not in votes:
        return "AGENTS_OPPOSED_ENTRY"
    return "AGENTS_ALIGNED_WITH_ENTRY"


def _find_opposing_overlap(trades: list[PaperTrace]) -> dict[str, bool]:
    flagged = {t.order_id: False for t in trades}
    by_symbol: dict[tuple[str, str], list[PaperTrace]] = defaultdict(list)
    for t in trades:
        by_symbol[(t.exchange, t.symbol)].append(t)
    for group in by_symbol.values():
        group.sort(key=lambda x: x.opened_at)
        for i, first in enumerate(group):
            for second in group[i+1:]:
                if second.opened_at >= (first.closed_at or datetime.max.replace(tzinfo=timezone.utc)):
                    break
                if first.side != second.side:
                    flagged[first.order_id] = True
                    flagged[second.order_id] = True
    return flagged


def analyze_paper_trades(
    records: Iterable[PaperTrace], *, minimum_samples: int = 30,
) -> ForensicsReport:
    """Read-only metrics and source-backed hypotheses; no agent score promotion."""
    if not 1 <= minimum_samples <= 100_000:
        raise ValueError("Invalid minimum sample count")
    rows = list(records)
    if len({row.order_id for row in rows}) != len(rows):
        raise ValueError("Duplicate PAPER order ID would double-count PnL")
    open_items = [r for r in rows if r.status == "FILLED"]
    closed_items = [r for r in rows if r.status == "CLOSED"]
    overlaps = _find_opposing_overlap(rows)
    findings = []
    counts: Counter[str] = Counter()
    for r in rows:
        flags: set[str] = set()
        direction = 1 if r.side == "BUY" else -1
        risk_amount = None
        reward_risk = None
        realized_r = None
        if r.stop_loss is None:
            flags.add("STOP_NOT_RECORDED")
        else:
            risk_distance = (r.entry_price - r.stop_loss) * direction
            if risk_distance <= 0:
                flags.add("INVALID_STOP_SIDE")
            else:
                risk_amount = r.position_notional * risk_distance / r.entry_price
                if r.status == "CLOSED":
                    realized_r = r.pnl_net / risk_amount
        if r.take_profit is None:
            flags.add("TARGET_NOT_RECORDED")
        else:
            target_distance = (r.take_profit - r.entry_price) * direction
            if target_distance <= 0:
                flags.add("INVALID_TARGET_SIDE")
            elif risk_amount is not None:
                reward_risk = target_distance / abs(r.entry_price - r.stop_loss)
                if reward_risk < 1.0:
                    flags.add("PLANNED_REWARD_BELOW_RISK")
        if overlaps[r.order_id]:
            flags.add("OPPOSING_POSITION_OVERLAP")
        if not r.decision_trace_present:
            flags.add("DECISION_TRACE_MISSING")
        elif r.decision_action is not None and r.decision_action != r.side:
            flags.add("DECISION_SIDE_MISMATCH")
        if not r.risk_trace_present:
            flags.add("RISK_TRACE_MISSING")
        elif r.risk_approved is False:
            flags.add("RISK_NOT_APPROVED")
        if r.status == "CLOSED" and not r.audit_close_present:
            flags.add("AUDIT_CLOSURE_MISSING")
        if r.audit_close_present and (r.exit_reason is None or r.exit_price is None):
            flags.add("EXIT_DETAIL_UNAVAILABLE")
        if r.status == "CLOSED" and r.exit_reason == "TAKE_PROFIT" and r.pnl_net < 0:
            flags.add("TAKE_PROFIT_NET_LOSS")
        vote = _agents_vote(r)
        if vote == "CONFLICTING_AGENT_OPINIONS":
            flags.add("AGENT_OPINION_CONFLICT")
        elif vote == "AGENTS_OPPOSED_ENTRY":
            flags.add("AGENT_OPPOSED_DIRECTION")
        for flag in flags:
            counts[flag] += 1
        evidence_complete = (
            r.status == "CLOSED" and r.audit_close_present
            and r.exit_reason is not None and r.exit_price is not None
            and r.decision_trace_present and r.risk_trace_present
        )
        findings.append(TradeFinding(
            order_id=r.order_id, strategy=r.strategy,
            symbol=r.symbol, timeframe=r.timeframe, side=r.side,
            realized_net=r.pnl_net,
            realized_r=round(realized_r, 5) if realized_r is not None else None,
            planned_risk_notional=(round(risk_amount, 6)
                                   if risk_amount is not None else None),
            planned_reward_risk=(round(reward_risk, 5)
                                 if reward_risk is not None else None),
            flags=tuple(sorted(flags)),
            evidence_status=("COMPLETE_OBSERVATIONAL" if evidence_complete
                             else "INCOMPLETE_OR_OPEN"),
            agent_agreement=vote,
        ))
    pnls = [r.pnl_net for r in closed_items]
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    pf = gains / losses if losses > 0 else None
    return ForensicsReport(
        closed_trades=len(closed_items),
        open_trades=len(open_items),
        net_pnl=round(sum(pnls), 6),
        wins=sum(x > 0 for x in pnls),
        win_rate=(round(sum(x > 0 for x in pnls)/len(pnls), 6)
                  if pnls else None),
        profit_factor=round(pf, 6) if pf is not None else None,
        profit_factor_status=(
            "NO_LOSSES_UNDEFINED" if losses == 0
            else "ZERO_PROFIT" if gains == 0 else "FINITE"
        ),
        missing_audit_closures=sum(not r.audit_close_present for r in closed_items),
        missing_decision_traces=sum(not r.decision_trace_present for r in rows),
        missing_risk_traces=sum(not r.risk_trace_present for r in rows),
        agent_attribution_verified=False,
        flags_count=dict(sorted(counts.items())),
        by_strategy=_group(closed_items, "strategy", minimum_samples),
        by_symbol=_group(closed_items, "symbol", minimum_samples),
        by_timeframe=_group(closed_items, "timeframe", minimum_samples),
        by_exit_reason=_group(closed_items, "exit_reason", minimum_samples),
        by_side=_group(closed_items, "side", minimum_samples),
        trades=tuple(findings),
        limitations=(
            "Agent output correlation is observational, not causal PnL attribution.",
            "Net PAPER PnL already includes execution deductions: never subtract fees/slippage twice.",
            "An anomaly flag proposes investigation, not proof that it caused a loss.",
            "Trade sample thresholds are research-policy defaults, not statistical guarantees.",
            "No candle/regime/ATR context is inferred without point-in-time market evidence.",
            "Historical retention/archive coverage must be independently verified.",
            "The one-way overlap flag assumes one shared exchange book; it does not prove execution mode.",
        ),
    )

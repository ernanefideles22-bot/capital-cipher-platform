"""Compile individual research-candidate decisions into one netted venue book.

No new trades are authorized: this translates PRECOMPUTED candidate trades
to the timestamped historical venue simulator. Reports can be compared only
on the same explicitly committed evaluation window and market-data archive.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping, Sequence

from app.strategy.candidate_replay import CandidateReplayReport
from app.strategy.venue_research_catalog import HistoricalTapeDataset, canonical_json
from app.strategy.venue_research_execution import (
    HistoricalPortfolioReport, OrderIntent, replay_venue_portfolio, utc,
)


@dataclass(frozen=True)
class StrategyTargetEvent:
    event_id: str
    strategy_key: str
    event_type: str
    received_at: datetime
    execution_at: datetime
    target_signed_contracts: int

    def __post_init__(self) -> None:
        if not self.event_id or not self.strategy_key:
            raise ValueError("Research event and strategy identities required")
        if self.event_type not in {"ENTRY", "EXIT"}:
            raise ValueError("Invalid strategy transition")
        if type(self.target_signed_contracts) is not int:
            raise ValueError("Signed target must use integer contracts")
        if self.event_type == "ENTRY" and self.target_signed_contracts == 0:
            raise ValueError("Entry must declare exposure")
        if self.event_type == "EXIT" and self.target_signed_contracts != 0:
            raise ValueError("Exit must target flat")
        if utc(self.received_at) > utc(self.execution_at):
            raise ValueError("Strategy signal was not available at execution")


@dataclass(frozen=True)
class CompiledPortfolioPlan:
    ordered_events: tuple[StrategyTargetEvent, ...]
    intents: tuple[OrderIntent, ...]
    zero_delta_events: int
    unhedge_events: int
    schedule_sha256: str
    execution_authorized: bool = False


@dataclass(frozen=True)
class VenueCandidateComparison:
    archive_sha256: str
    schedule_sha256: str
    baseline_portfolio_net: float
    candidate_portfolio_net: float
    incremental_net: float
    baseline_fills: int
    candidate_fills: int
    baseline_funding_cost: float
    candidate_funding_cost: float
    interpretation: str = "RESEARCH_ONLY_NOT_OOS_ATTESTATION"
    execution_authorized: bool = False


def candidate_trades_to_targets(
    reports: Sequence[CandidateReplayReport], *,
    contract_multiplier: float, expected_symbol: str,
) -> tuple[StrategyTargetEvent, ...]:
    """Convert D2 independently simulated trades to desired strategy exposures.

    Risk/quantity is rounded DOWN to the venue's whole-contract unit.
    STOP_LOSS/TAKE_PROFIT signals are assumed to be standing brackets already
    present at entry; their intrabar timing remains only OHLCV-estimated.
    """
    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0:
        raise ValueError("Positive contract multiplier required")
    seen: set[str] = set()
    events: list[StrategyTargetEvent] = []
    for report in reports:
        if report.fingerprint in seen:
            raise ValueError("Repeated strategy report in one portfolio")
        seen.add(report.fingerprint)
        if report.symbol != expected_symbol or report.status != "RESEARCH_ONLY":
            raise ValueError("Untrusted strategy symbol or status")
        if report.execution_authorized:
            raise ValueError("Research report unexpectedly carries authority")
        for idx, trade in enumerate(report.trades):
            if trade.entry_signal_received_at is None or trade.exit_signal_received_at is None:
                raise ValueError("Missing causal signal availability evidence")
            if trade.side not in ("BUY", "SELL"):
                raise ValueError("Unrecognized strategy side")
            if not math.isfinite(trade.quantity) or trade.quantity <= 0:
                raise ValueError("Nonpositive or nonfinite candidate quantity")
            contracts = math.floor(trade.quantity / contract_multiplier)
            if contracts < 1:
                raise ValueError("Candidate trade is smaller than one venue contract")
            if utc(trade.entry_at) >= utc(trade.exit_at):
                raise ValueError("Trade must exit after entry")
            if utc(trade.entry_signal_received_at) > utc(trade.entry_at):
                raise ValueError("Entry signal was not available")
            if utc(trade.exit_signal_received_at) > utc(trade.exit_at):
                raise ValueError("Exit signal was not available")
            signed = contracts if trade.side == "BUY" else -contracts
            strategy_key = report.fingerprint
            events.extend([
                StrategyTargetEvent(
                    event_id=f"{strategy_key}:{idx}:ENTRY",
                    strategy_key=strategy_key, event_type="ENTRY",
                    received_at=trade.entry_signal_received_at,
                    execution_at=trade.entry_at,
                    target_signed_contracts=signed,
                ),
                StrategyTargetEvent(
                    event_id=f"{strategy_key}:{idx}:EXIT",
                    strategy_key=strategy_key, event_type="EXIT",
                    received_at=trade.exit_signal_received_at,
                    execution_at=trade.exit_at,
                    target_signed_contracts=0,
                ),
            ])
    return tuple(events)


def compile_oneway_targets(
    events: Sequence[StrategyTargetEvent], *, contract: str,
    max_contracts: int, allow_unhedge_on_exit: bool = False,
    allow_reversal: bool = False,
) -> CompiledPortfolioPlan:
    """Deterministic shared target, exit-before-entry on ties.

    Exit from a hedged sub-book can INCREASE a real net position. Fail closed
    unless the research caller explicitly opts into that non-reduce-only
    rebalance. A true stop exchange order may behave differently.
    """
    if not contract or max_contracts < 1:
        raise ValueError("Invalid contract metadata")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("Duplicate research source event identity")
    ordered = tuple(sorted(
        events, key=lambda event: (
            utc(event.execution_at), 0 if event.event_type == "EXIT" else 1,
            event.strategy_key, event.event_id,
        ),
    ))
    target_by_strategy: dict[str, int] = {}
    intents: list[OrderIntent] = []
    zero_delta = unhedge_count = 0
    current = 0
    for event in ordered:
        old_strategy = target_by_strategy.get(event.strategy_key, 0)
        if event.event_type == "ENTRY" and old_strategy != 0:
            raise ValueError("Repeated entry while strategy already holds exposure")
        if event.event_type == "EXIT" and old_strategy == 0:
            raise ValueError("Exit without an existing strategy position")
        next_value = event.target_signed_contracts
        proposed = current - old_strategy + next_value
        if abs(proposed) > max_contracts:
            raise ValueError("Combined strategy contract limit exceeded")
        if current * proposed < 0 and not allow_reversal:
            raise ValueError("One-way reversal requires explicit research policy")
        unhedge = event.event_type == "EXIT" and abs(proposed) > abs(current)
        if unhedge and not allow_unhedge_on_exit:
            raise ValueError("Closing a hedge would increase net exposure")
        if unhedge:
            unhedge_count += 1
        delta = proposed - current
        if delta:
            reducing = (
                current != 0
                and (proposed == 0 or current * proposed > 0)
                and abs(proposed) < abs(current)
            )
            intents.append(OrderIntent(
                event_id="venue:" + event.event_id,
                strategy=event.strategy_key,
                contract=contract,
                side="BUY" if delta > 0 else "SELL",
                contracts=abs(delta),
                signal_received_at=utc(event.received_at),
                execution_at=utc(event.execution_at),
                sequence=len(intents),
                reduce_only=reducing,
            ))
        else:
            zero_delta += 1
        target_by_strategy[event.strategy_key] = next_value
        current = proposed
    if current or any(target_by_strategy.values()):
        raise ValueError("Unclosed strategy targets: cannot claim final realized PnL")
    digest = hashlib.sha256(canonical_json({
        "contract": contract,
        "events": [
            {**asdict(e), "received_at":utc(e.received_at).isoformat(),
             "execution_at":utc(e.execution_at).isoformat()} for e in ordered
        ],
        "allow_unhedge": allow_unhedge_on_exit,
        "allow_reversal": allow_reversal,
    }).encode()).hexdigest()
    return CompiledPortfolioPlan(
        ordered_events=ordered, intents=tuple(intents),
        zero_delta_events=zero_delta, unhedge_events=unhedge_count,
        schedule_sha256=digest,
    )


def run_candidate_portfolio(
    archive: HistoricalTapeDataset,
    reports: Sequence[CandidateReplayReport], *,
    starting_equity: float = 10000,
    allow_unhedge_on_exit: bool = False,
) -> tuple[CompiledPortfolioPlan, HistoricalPortfolioReport]:
    """Run actual independently emitted candidate trades on one venue tape.

    Requires upfront externally sourced historical quote and funding archive.
    """
    if not reports:
        raise ValueError("At least one candidate report required")
    spec = archive.spec
    expected_symbol = spec.symbol.replace("_", "")
    targets = candidate_trades_to_targets(
        reports, contract_multiplier=spec.multiplier,
        expected_symbol=expected_symbol,
    )
    compiled = compile_oneway_targets(
        targets, contract=spec.symbol, max_contracts=spec.maximum_contracts,
        allow_unhedge_on_exit=allow_unhedge_on_exit,
    )
    venue = replay_venue_portfolio(
        archive.tape(), compiled.intents,
        funding_events=archive.funding, starting_equity=starting_equity,
    )
    if venue.final_state.signed_contracts:
        raise ValueError("Research portfolio must end flat")
    return compiled, venue


def compare_candidate_to_baseline(
    archive: HistoricalTapeDataset,
    baseline: Sequence[CandidateReplayReport],
    candidate: Sequence[CandidateReplayReport],
    *, evaluation_start_at: datetime, evaluation_end_at: datetime,
    starting_equity: float = 10000,
    allow_unhedge_on_exit: bool = False,
) -> VenueCandidateComparison:
    """Same committed venue tape; one-way accounting for each alternative.

    This is a paired execution *scenario*, NOT a valid independent sealed
    holdout; original agents' own lookahead/OOS provenance must be attested.
    """
    if not baseline or not candidate:
        raise ValueError("Both portfolios require candidate strategy reports")
    start, end = utc(evaluation_start_at), utc(evaluation_end_at)
    if start >= end:
        raise ValueError("Invalid shared evaluation window")
    if (utc(archive.quotes[0].received_at) > start
            or utc(archive.quotes[-1].received_at) < end):
        raise ValueError("Venue tape does not span the common evaluation window")
    for report in (*baseline, *candidate):
        if (report.evaluation_starts_at is None
                or report.evaluation_ends_at is None):
            raise ValueError("Missing signed evaluation window from candidate replay")
        if (utc(report.evaluation_starts_at) > start
                or utc(report.evaluation_ends_at) < end):
            raise ValueError("Candidate does not cover the common evaluation window")
        if any(utc(trade.entry_at) < start or utc(trade.exit_at) > end
               for trade in report.trades):
            raise ValueError("Candidate trades extend beyond common evaluation window")
    bp, br = run_candidate_portfolio(
        archive, baseline, starting_equity=starting_equity,
        allow_unhedge_on_exit=allow_unhedge_on_exit,
    )
    cp, cr = run_candidate_portfolio(
        archive, candidate, starting_equity=starting_equity,
        allow_unhedge_on_exit=allow_unhedge_on_exit,
    )
    return VenueCandidateComparison(
        archive_sha256=archive.manifest.dataset_sha256,
        schedule_sha256=hashlib.sha256(
            f"{bp.schedule_sha256}:{cp.schedule_sha256}".encode()
        ).hexdigest(),
        baseline_portfolio_net=br.final_state.realized_net,
        candidate_portfolio_net=cr.final_state.realized_net,
        incremental_net=round(
            cr.final_state.realized_net - br.final_state.realized_net, 10
        ),
        baseline_fills=len(br.fills), candidate_fills=len(cr.fills),
        baseline_funding_cost=br.final_state.funding_cost,
        candidate_funding_cost=cr.final_state.funding_cost,
    )

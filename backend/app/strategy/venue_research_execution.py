"""Offline Gate-like one-way netting and point-in-time quote execution for research.

NO network, broker, OMS or PAPER production imports. All inputs are historical
observations supplied by an independent dataset loader. This is a strict
one-way accounting model, NOT proof of Gate's matching-engine equivalence.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from typing import Sequence


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware event time required")
    return value.astimezone(timezone.utc)


def finite_positive(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class ContractSpec:
    """Snapshot of Gate contract metadata; contract units are integers."""
    symbol: str
    multiplier: float
    tick_size: float
    maximum_contracts: int = 100000

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.endswith("_USDT"):
            raise ValueError("Expected explicit Gate futures USDT contract identifier")
        finite_positive(self.multiplier, "contract multiplier")
        finite_positive(self.tick_size, "price tick")
        if self.maximum_contracts < 1:
            raise ValueError("Contract limit must be positive")


@dataclass(frozen=True)
class Quote:
    quote_id: str
    contract: str
    observed_at: datetime
    received_at: datetime
    bid: float
    ask: float
    bid_depth_contracts: int
    ask_depth_contracts: int
    taker_fee_bps: float

    def __post_init__(self) -> None:
        if not self.quote_id:
            raise ValueError("Missing source quote identity")
        if utc(self.received_at) < utc(self.observed_at):
            raise ValueError("Quote observed after it was received")
        finite_positive(self.bid, "bid")
        finite_positive(self.ask, "ask")
        if self.bid >= self.ask:
            raise ValueError("Crossed or locked market quote")
        if min(self.bid_depth_contracts, self.ask_depth_contracts) < 1:
            raise ValueError("Missing executable top-of-book depth")
        if not isfinite(self.taker_fee_bps) or not 0 <= self.taker_fee_bps <= 1000:
            raise ValueError("Invalid historical taker fee")


@dataclass(frozen=True)
class FundingEvent:
    funding_id: str
    contract: str
    settled_at: datetime
    received_at: datetime
    mark_price: float
    rate: float

    def __post_init__(self) -> None:
        if not self.funding_id:
            raise ValueError("Funding source identity required")
        utc(self.received_at)
        utc(self.settled_at)
        finite_positive(self.mark_price, "funding mark")
        if not isfinite(self.rate) or abs(self.rate) > 0.1:
            raise ValueError("Historical funding rate out of bounded research range")


@dataclass(frozen=True)
class OrderIntent:
    event_id: str
    strategy: str
    contract: str
    side: str
    contracts: int
    signal_received_at: datetime
    execution_at: datetime
    sequence: int
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if not self.event_id or not self.strategy:
            raise ValueError("Order intent identity required")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("Invalid signed direction")
        if type(self.contracts) is not int or self.contracts <= 0:
            raise ValueError("Contract size must be a positive integer")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Deterministic nonnegative execution sequence required")
        if utc(self.signal_received_at) > utc(self.execution_at):
            raise ValueError("Cannot execute before the signal was available")


@dataclass(frozen=True)
class HistoricalFill:
    event_id: str
    strategy: str
    contract: str
    time: datetime
    quote_id: str
    side: str
    contracts: int
    price: float
    fee: float
    spread_cost: float
    adverse_slippage_cost: float


class HistoricalQuoteTape:
    """Select the newest receipt available at execution time, never a future quote."""

    def __init__(self, spec: ContractSpec, quotes: Sequence[Quote], *,
                 max_age_seconds: int = 60, extra_slippage_bps: float = 12.0,
                 impact_bps_per_extra_top_level: float = 10.0) -> None:
        if max_age_seconds < 1 or max_age_seconds > 3600:
            raise ValueError("Unsupported quote max-age")
        if not all(isfinite(x) and 0 <= x <= 1000 for x in (
            extra_slippage_bps, impact_bps_per_extra_top_level
        )):
            raise ValueError("Invalid execution stress")
        if extra_slippage_bps < 12:
            raise ValueError("Research requires >=12 bps extra slippage stress")
        if not quotes:
            raise ValueError("No historical quotes")
        previous = None
        seen = set()
        for q in quotes:
            if q.contract != spec.symbol:
                raise ValueError("Mixed Gate contract")
            at = utc(q.received_at)
            if previous is not None and at <= previous:
                raise ValueError("Quotes must be unique and ordered by receipt")
            if q.quote_id in seen:
                raise ValueError("Duplicate quote identity")
            seen.add(q.quote_id)
            previous = at
        self.spec = spec
        self.quotes = tuple(quotes)
        self.receipts = tuple(utc(q.received_at) for q in quotes)
        self.max_age_seconds = max_age_seconds
        self.extra_slippage_bps = extra_slippage_bps
        self.impact_bps = impact_bps_per_extra_top_level

    def at(self, instant: datetime) -> Quote:
        at = utc(instant)
        index = bisect_right(self.receipts, at) - 1
        if index < 0:
            raise ValueError("No historically available quote")
        quote = self.quotes[index]
        age = (at - utc(quote.observed_at)).total_seconds()
        if age < 0 or age > self.max_age_seconds:
            raise ValueError("Historical quote is future-dated or stale")
        return quote

    def simulate(self, intent: OrderIntent) -> HistoricalFill:
        if intent.contract != self.spec.symbol:
            raise ValueError("Contract mismatch")
        if intent.contracts > self.spec.maximum_contracts:
            raise ValueError("Order exceeds contract metadata limit")
        quote = self.at(intent.execution_at)
        side_buy = intent.side == "BUY"
        reference = quote.ask if side_buy else quote.bid
        available = (quote.ask_depth_contracts if side_buy
                     else quote.bid_depth_contracts)
        excess = max(0, intent.contracts / available - 1.0)
        adverse_bps = self.extra_slippage_bps + excess * self.impact_bps
        if adverse_bps > 1000:
            raise ValueError("Impact exceeds conservative research guard")
        price = reference * (1 + adverse_bps / 10000
                             if side_buy else 1 - adverse_bps / 10000)
        if price <= 0:
            raise ValueError("Invalid simulated execution price")
        # Adverse rounding to the contract tick; never better than quote.
        import math
        tick = self.spec.tick_size
        units = price / tick
        price = (math.ceil(units - 1e-10) if side_buy
                 else math.floor(units + 1e-10)) * tick
        price = round(price, 10)
        value = intent.contracts * self.spec.multiplier
        mid = (quote.bid + quote.ask) / 2
        notional = value * price
        spread_cost = value * abs(reference - mid)
        adverse_slippage = value * abs(price - reference)
        return HistoricalFill(
            event_id=intent.event_id, strategy=intent.strategy,
            contract=intent.contract, time=utc(intent.execution_at),
            quote_id=quote.quote_id, side=intent.side,
            contracts=intent.contracts, price=price,
            fee=round(notional * quote.taker_fee_bps / 10000, 10),
            spread_cost=round(spread_cost, 10),
            adverse_slippage_cost=round(adverse_slippage, 10),
        )


@dataclass
class Position:
    signed_contracts: int = 0
    average_price: float = 0.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    signed_contracts: int
    average_entry_price: float
    realized_gross: float
    fees: float
    funding_cost: float
    realized_net: float
    last_event: str | None
    handled_orders: int
    handled_funding: int


class OneWayPortfolio:
    """Single netted contract position across SCALP/DAY/SWING.

    Signed contracts: BUY positive, SELL negative. Opposite fills first
    realize existing inventory, then may flip into the other direction;
    reduce_only never increases or reverses exposure.
    """

    def __init__(self, spec: ContractSpec, *, starting_equity: float = 10000.0,
                 leverage: float = 1.0) -> None:
        finite_positive(starting_equity, "starting equity")
        if leverage != 1.0:
            raise ValueError("Research portfolio is locked at 1x")
        self.spec = spec
        self.starting_equity = starting_equity
        self.position = Position()
        self.realized_gross = self.fees = self.funding_cost = 0.0
        self._seen_orders: set[str] = set()
        self._seen_funding: set[str] = set()
        self._last_time: datetime | None = None
        self._last_event: str | None = None

    def _require_monotonic(self, at: datetime) -> None:
        instant = utc(at)
        if self._last_time is not None and instant < self._last_time:
            raise ValueError("Portfolio events out of chronological order")

    def apply_fill(self, intent: OrderIntent, fill: HistoricalFill) -> PortfolioSnapshot:
        if intent.event_id in self._seen_orders:
            raise ValueError("Duplicate order identity")
        if intent.contract != self.spec.symbol or fill.contract != self.spec.symbol:
            raise ValueError("Portfolio contract mismatch")
        if (fill.event_id != intent.event_id or fill.strategy != intent.strategy
            or fill.side != intent.side or fill.contracts != intent.contracts
            or utc(fill.time) != utc(intent.execution_at)):
            raise ValueError("Fill does not belong to the intent")
        if not all(isfinite(x) and x >= 0 for x in
                   (fill.fee, fill.spread_cost, fill.adverse_slippage_cost)):
            raise ValueError("Invalid fill costs")
        finite_positive(fill.price, "fill price")
        self._require_monotonic(fill.time)
        old = self.position.signed_contracts
        delta = intent.contracts * (1 if intent.side == "BUY" else -1)
        new = old + delta
        if intent.reduce_only and (
            old == 0 or old * delta >= 0 or abs(new) >= abs(old)
            or old * new < 0
        ):
            raise ValueError("reduce_only cannot open, increase or reverse")
        if abs(new) > self.spec.maximum_contracts:
            raise ValueError("Position contract cap exceeded")
        available_equity = self.starting_equity + (
            self.realized_gross - self.fees - self.funding_cost - fill.fee
        )
        if abs(new) * fill.price * self.spec.multiplier > max(available_equity, 0) + 1e-7:
            raise ValueError("Insufficient isolated 1x research equity")
        average = self.position.average_price
        gross = 0.0
        if old == 0:
            next_average = fill.price
        elif old * delta > 0:
            next_average = (abs(old) * average + abs(delta) * fill.price) / abs(new)
        else:
            close_qty = min(abs(old), abs(delta))
            gross = close_qty * self.spec.multiplier * (
                fill.price - average
            ) * (1 if old > 0 else -1)
            next_average = 0.0 if new == 0 else (
                average if old * new > 0 else fill.price
            )
        self.position = Position(new, next_average)
        self.realized_gross += gross
        self.fees += fill.fee
        self._seen_orders.add(intent.event_id)
        self._last_time = utc(fill.time)
        self._last_event = intent.event_id
        return self.snapshot()

    def apply_funding(self, event: FundingEvent) -> PortfolioSnapshot:
        if event.funding_id in self._seen_funding:
            raise ValueError("Duplicate funding event")
        if event.contract != self.spec.symbol:
            raise ValueError("Funding contract mismatch")
        self._require_monotonic(event.settled_at)
        # Do not incorporate an outcome that would be unavailable at the
        # replay timestamp. Historical event timestamps must be attested.
        if utc(event.received_at) > utc(event.settled_at):
            raise ValueError("Funding not known at simulated settlement")
        cost = (self.position.signed_contracts * self.spec.multiplier
                * event.mark_price * event.rate)
        self.funding_cost += cost
        self._seen_funding.add(event.funding_id)
        self._last_time = utc(event.settled_at)
        self._last_event = event.funding_id
        return self.snapshot()

    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            signed_contracts=self.position.signed_contracts,
            average_entry_price=round(self.position.average_price, 10),
            realized_gross=round(self.realized_gross, 10),
            fees=round(self.fees, 10),
            funding_cost=round(self.funding_cost, 10),
            realized_net=round(
                self.realized_gross - self.fees - self.funding_cost, 10
            ),
            last_event=self._last_event,
            handled_orders=len(self._seen_orders),
            handled_funding=len(self._seen_funding),
        )


@dataclass(frozen=True)
class HistoricalPortfolioReport:
    fills: tuple[HistoricalFill, ...]
    snapshots: tuple[PortfolioSnapshot, ...]
    final_state: PortfolioSnapshot
    execution_authorized: bool = False
    mode: str = "OFFLINE_ONE_WAY_RESEARCH"


def replay_venue_portfolio(
    tape: HistoricalQuoteTape, intents: Sequence[OrderIntent], *,
    funding_events: Sequence[FundingEvent] = (),
    starting_equity: float = 10000.0,
) -> HistoricalPortfolioReport:
    """Synchronous finite historical event replay; never call external APIs.

    Replay funding at historical settlement times, and ordered order intents
    at their future execution timestamps. Two order intents at the same
    timestamp REQUIRE distinct predeclared sequence numbers.
    """
    portfolio = OneWayPortfolio(tape.spec, starting_equity=starting_equity)
    events: list[tuple[datetime, int, int, object]] = []
    seen_sequence: set[tuple[datetime, int]] = set()
    for intent in intents:
        key = (utc(intent.execution_at), intent.sequence)
        if key in seen_sequence:
            raise ValueError("Ambiguous competing strategies at one timestamp")
        seen_sequence.add(key)
        events.append((key[0], 1, intent.sequence, intent))
    for funding in funding_events:
        events.append((utc(funding.settled_at), 0, 0, funding))
    events.sort(key=lambda x: (x[0], x[1], x[2]))
    fills: list[HistoricalFill] = []
    snapshots: list[PortfolioSnapshot] = []
    for _, category, _, item in events:
        if category == 0:
            snapshots.append(portfolio.apply_funding(item))
        else:
            fill = tape.simulate(item)
            snapshots.append(portfolio.apply_fill(item, fill))
            fills.append(fill)
    return HistoricalPortfolioReport(
        fills=tuple(fills), snapshots=tuple(snapshots),
        final_state=portfolio.snapshot(),
    )

"""S3 experimental prospective SHADOW PnL scorer, zero exchange/OMS authority.

A signal is observed AFTER its candle's closed_at; a hypothetical fill
is permitted ONLY at a later candle OPEN strictly after that observation.
Bar OHLCV are received after closure; simulation is not executable pricing.
No credentials, broker, persistence, orders, account balances or agent updates.
S3 is a scorecard, NOT a capital deployment or trading approval.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from typing import Mapping

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_replay import (
    Friction, ResearchRisk, _adverse_execution, _exit_trigger, _OpenPosition,
)
from app.strategy.gate_s2d_forward import FROZEN_AT, POLICY_SOURCE_COMMIT
from app.strategy.gate_s2e_live_shadow import S2E_POLICY, S2E_POLICY_COMMIT

PERIOD_SECONDS=900
ARMS=("STANDALONE","S1_CONFLUENCE")
MAX_CATCHUP_SECONDS=900


@dataclass(frozen=True)
class _Pending:
    direction: int
    action: str
    decision_close: datetime
    observed_at: datetime
    distance: float
    target_distance: float


@dataclass
class _Book:
    equity: float
    peak_equity: float
    max_drawdown_pct: float=0.
    daily_start_equity: float=0.
    day: object=None
    pending: _Pending | None=None
    position: _OpenPosition | None=None
    entry_candle: datetime | None=None
    completed_trades: int=0
    wins: int=0
    gross_before_fees: float=0.
    fees: float=0.
    funding: float=0.
    net: float=0.
    stops: int=0
    targets: int=0
    skipped_due_to_risk: int=0
    expired_signals: int=0


class ShadowForwardScorecard:
    def __init__(self,*,friction:Friction=Friction(),
                 risk:ResearchRisk=ResearchRisk()):
        self.friction=friction
        self.risk=risk
        self._books:dict[tuple[str,str,str],_Book]={}
        self._last:dict[str,datetime]={}
        self._digest:dict[str,str]={}
        self._last_observed:dict[str,datetime]={}
        self._halted:set[str]=set()
        self._events:list[dict]=[]

    def _book(self,symbol,strategy,arm):
        key=(symbol,strategy,arm)
        if key not in self._books:
            self._books[key]=_Book(
                equity=self.risk.initial_equity,
                peak_equity=self.risk.initial_equity,
                daily_start_equity=self.risk.initial_equity)
        return self._books[key]

    def _close(self,book,position,bar,raw_price,reason):
        direction=position.direction
        price=_adverse_execution(raw_price,direction,opening=False,
                                 friction=self.friction)
        gross=(price-position.entry)*position.quantity*direction
        fees=(price+position.entry)*position.quantity*self.friction.fee_bps_side/10000
        elapsed=(bar.closed_at-book.entry_candle).total_seconds()
        intervals=max(1,ceil(max(0.,elapsed)/28800))
        funding=(position.entry*position.quantity*
                 self.friction.funding_bps_8h/10000*intervals)
        net=gross-fees-funding
        book.equity+=net
        book.net+=net
        book.gross_before_fees+=gross
        book.fees+=fees
        book.funding+=funding
        book.completed_trades+=1
        book.wins+=net>0
        book.stops+=reason.startswith("STOP")
        book.targets+=reason.startswith("TARGET")
        book.peak_equity=max(book.peak_equity,book.equity)
        book.max_drawdown_pct=max(
            book.max_drawdown_pct,
            (1-book.equity/book.peak_equity)*100)
        book.position=None
        book.entry_candle=None
        return reason

    def _step(self,book,bar,strategy):
        utc_day=bar.closed_at.astimezone(timezone.utc).date()
        if book.day!=utc_day:
            book.day=utc_day
            book.daily_start_equity=book.equity
        if book.position is not None:
            bars_held=(bar.closed_at-book.entry_candle).total_seconds()//PERIOD_SECONDS
            if bars_held>=STRATEGIES[strategy].max_hold_bars:
                reason=self._close(book,book.position,bar,bar.open,
                                   "TIME_LIMIT_NEXT_OPEN")
                return reason
            trigger=_exit_trigger(bar,book.position)
            if trigger:
                return self._close(book,book.position,bar,*trigger)
            return "OPEN_POSITION"
        pending=book.pending
        if pending is None:
            return "NO_PENDING"
        bar_open=bar.closed_at.timestamp()-PERIOD_SECONDS
        if bar_open<=pending.observed_at.timestamp():
            return "PENDING_FUTURE_OPEN"
        book.pending=None
        age=bar_open-pending.decision_close.timestamp()
        if age<0 or age>MAX_CATCHUP_SECONDS:
            book.expired_signals+=1
            return "PENDING_EXPIRED"
        if (book.equity<=0
                or book.equity<book.daily_start_equity*
                         (1-self.risk.max_daily_loss_pct/100)
                or book.equity<book.peak_equity*
                         (1-self.risk.max_drawdown_pct/100)):
            book.skipped_due_to_risk+=1
            return "RISK_BLOCKED"
        entry=_adverse_execution(bar.open,pending.direction,opening=True,
                                 friction=self.friction)
        if entry<=pending.distance:
            book.skipped_due_to_risk+=1
            return "INVALID_STOP"
        quantity=min(
            book.equity*self.risk.risk_per_trade_pct/100/pending.distance,
            book.equity*self.risk.max_notional_pct/100/entry)
        if not math.isfinite(quantity) or quantity<=0:
            book.skipped_due_to_risk+=1
            return "INVALID_SIZE"
        position=_OpenPosition(
            strategy_id=strategy,side=pending.action,
            direction=pending.direction,decision_index=-1,entered_index=-1,
            entry=entry,quantity=quantity,
            stop=entry-pending.direction*pending.distance,
            target=entry+pending.direction*pending.target_distance)
        book.position=position
        book.entry_candle=bar.closed_at
        trigger=_exit_trigger(bar,position)
        if trigger:
            return self._close(book,position,bar,*trigger)
        return "PAPER_OPEN"

    @staticmethod
    def _validate(candle:Candle,shadow:Mapping,observed_at:datetime):
        if (candle.exchange!=Exchange.GATE
                or candle.symbol not in ("BTCUSDT","ETHUSDT","SOLUSDT")
                or candle.timeframe!="15m"
                or candle.received_at<candle.closed_at
                or candle.received_at>observed_at
                or candle.closed_at>=observed_at):
            raise ValueError("Invalid prospective candle receipt")
        if (shadow.get("status")!="READ_ONLY_CONFLUENCE_READY"
                or shadow.get("execution_authorized") is not False
                or shadow.get("source_decision_cohort_modified") is not False
                or shadow.get("oms_touched") is not False
                or shadow.get("policy_id")!=S2E_POLICY
                or shadow.get("policy_commit")!=S2E_POLICY_COMMIT
                or shadow.get("policy_commit")!=POLICY_SOURCE_COMMIT
                or shadow.get("symbol")!=candle.symbol
                or shadow.get("as_of_utc")!=observed_at.isoformat()):
            raise ValueError("Untrusted S2E decision provenance")
        rows=shadow.get("hypotheses")
        if not isinstance(rows,list) or len(rows)!=len(STRATEGIES):
            raise ValueError("Incomplete frozen strategy roster")
        seen=set()
        for row in rows:
            strategy=row.get("strategy_id")
            proposed=row.get("proposed_action")
            filtered=row.get("filtered_action")
            if (strategy in seen or strategy not in STRATEGIES
                    or row.get("strategy_version")!=STRATEGIES[strategy].version
                    or row.get("timeframe")!="15m"
                    or row.get("execution_authorized") is not False
                    or row.get("predictive_edge_proven") is not False
                    or proposed not in ("WAIT","LONG","SHORT")
                    or filtered not in ("WAIT",proposed)):
                raise ValueError("Invalid S2E hypothesis contract")
            if proposed!="WAIT":
                for k in ("stop_distance","target_distance"):
                    d=row.get(k)
                    if isinstance(d,bool) or not isinstance(d,(int,float)) or not math.isfinite(d) or d<=0:
                        raise ValueError("Invalid frozen stop/target")
            seen.add(strategy)

    def observe(self,candle:Candle,shadow:Mapping,*,observed_at:datetime)->dict:
        """Accept ONE read-only observation. Duplicate identical is idempotent.

        A discontinuity HALTS that symbol instead of assuming missing fills.
        No forward 'fills' use a bar opening before the signal was observed.
        """
        if (observed_at.tzinfo is None or observed_at.utcoffset() is None):
            raise ValueError("Timezone-aware observation timestamp required")
        observed_at=observed_at.astimezone(timezone.utc)
        self._validate(candle,shadow,observed_at)
        symbol=candle.symbol
        if symbol in self._halted:
            raise ValueError("Forward scorer halted on candle discontinuity")
        payload={
            "candle":candle.model_dump(mode="json"),
            "policy":shadow["policy_commit"],
            "as_of":observed_at.isoformat(),
            "hypotheses":shadow["hypotheses"],
        }
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,
                           separators=(",",":"),allow_nan=False).encode()).hexdigest()
        prior=self._last.get(symbol)
        if prior is not None:
            if candle.closed_at==prior:
                if self._digest[symbol]!=digest:
                    raise ValueError("Conflicting duplicate observation")
                return self.snapshot(symbol,duplicate=True)
            if observed_at<=self._last_observed[symbol]:
                raise ValueError("Out-of-order observation receipt")
            if (candle.closed_at-prior).total_seconds()!=PERIOD_SECONDS:
                self._halted.add(symbol)
                raise ValueError("Missing or out-of-order candle; paper scorer halted")
        # Do not model precommit results as prospective evaluation.
        if candle.closed_at<=FROZEN_AT:
            self._last[symbol]=candle.closed_at
            self._digest[symbol]=digest
            self._last_observed[symbol]=observed_at
            return self.snapshot(symbol)
        decisions={x["strategy_id"]:x for x in shadow["hypotheses"]}
        updates=[]
        for strategy in sorted(STRATEGIES):
            row=decisions[strategy]
            for arm in ARMS:
                book=self._book(symbol,strategy,arm)
                event=self._step(book,candle,strategy)
                action=(row["proposed_action"] if arm=="STANDALONE"
                        else row["filtered_action"])
                if book.position is None and book.pending is None and action in ("LONG","SHORT"):
                    # An outcome must use a FUTURE price bar whose OPEN occurs
                    # strictly after observed_at, never this already closed bar.
                    book.pending=_Pending(
                        direction=1 if action=="LONG" else -1,
                        action=action,decision_close=candle.closed_at,
                        observed_at=observed_at,
                        distance=float(row["stop_distance"]),
                        target_distance=float(row["target_distance"]))
                updates.append({"strategy":strategy,"arm":arm,"event":event})
        self._last[symbol]=candle.closed_at
        self._digest[symbol]=digest
        self._last_observed[symbol]=observed_at
        self._events.append({
            "symbol":symbol,"candle":candle.closed_at.isoformat(),
            "observed_at":observed_at.isoformat(),"updates":updates,
        })
        return self.snapshot(symbol)

    def snapshot(self,symbol:str,*,duplicate:bool=False)->dict:
        results=[]
        for (sym,strategy,arm),b in sorted(self._books.items()):
            if sym!=symbol:continue
            results.append({
                "strategy":strategy,"arm":arm,
                "completed_trades":b.completed_trades,
                "wins":b.wins,
                "net_pnl":round(b.net,8),
                "gross_after_slippage_before_fees":round(b.gross_before_fees,8),
                "fees":round(b.fees,8),"funding":round(b.funding,8),
                "realized_only_drawdown_pct":round(b.max_drawdown_pct,6),
                "pending":b.pending is not None,
                "open_position":b.position is not None,
                "expired_signals":b.expired_signals,
                "risk_blocks":b.skipped_due_to_risk,
                "stops":b.stops,"targets":b.targets,
                "initial_equity":self.risk.initial_equity,
            })
        return {
            "stage":"PRO_TRADER_S3_FORWARD_SCORECARD",
            "status":"HALTED" if symbol in self._halted else "RESEARCH_ONLY",
            "symbol":symbol,"last_candle":
                self._last[symbol].isoformat() if symbol in self._last else None,
            "duplicate":duplicate,"accounts":results,
            "cash_benchmark_pnl":0.,
            "persistent_results":False,
            "unrealized_pnl_included":False,
            "simulated_entry_after_actual_receipt":True,
            "real_execution_proven":False,
            "profitability_proven":False,
            "execution_authorized":False,
        }

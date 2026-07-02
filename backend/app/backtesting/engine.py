"""Backtesting Engine (docs/17-backtesting-engine.md).

Replays historical candles sequentially through the same pipeline used in
paper trading (agents -> decision engine -> risk -> simulated execution),
guaranteeing no lookahead: agents only ever see candles already replayed.

A good backtest does not authorize live trading — it only authorizes further
investigation in paper trading (docs/17 final rule).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.trend import TrendAgent
from app.audit.service import AuditService
from app.core.event_bus import EventBus
from app.core.logging import ServiceLogger
from app.core.state_machine import SystemState, SystemStateMachine
from app.market_data.store import CandleStore
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.service import Orchestrator
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.backtest import BacktestReport, BacktestRequest
from app.schemas.market import Candle
from app.schemas.risk import RiskLimits
from app.strategy.engine import StrategyEngine

logger = ServiceLogger("backtesting")


async def _paper_state_machine() -> SystemStateMachine:
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="backtest boot", actor="backtester")
    await sm.transition(SystemState.PAPER, reason="backtest ready", actor="backtester")
    return sm


class BacktestingEngine:
    """Runs isolated backtests with a fresh pipeline per run."""

    def __init__(
        self,
        *,
        limits: RiskLimits | None = None,
        initial_balance: float = 10_000.0,
        fee_rate_percent: float = 0.08,
        slippage_rate_percent: float = 0.02,
        strategy_engine: StrategyEngine | None = None,
    ) -> None:
        self._limits = limits or RiskLimits()
        self._initial_balance = initial_balance
        self._fee_rate = fee_rate_percent
        self._slippage_rate = slippage_rate_percent
        self._strategy_engine = strategy_engine
        self.reports: list[BacktestReport] = []

    async def run(self, request: BacktestRequest, candles: list[Candle]) -> BacktestReport:
        started = time.monotonic()
        if not candles:
            raise ValueError("Backtest requires at least one candle")

        # Candles must be processed in temporal order (docs/17, docs/32).
        ordered = sorted(candles, key=lambda c: c.closed_at)

        sm = await _paper_state_machine()
        audit = AuditService()
        store = CandleStore()
        risk = RiskManager(self._limits, sm, audit, initial_balance=self._initial_balance)
        paper = PaperTradingEngine(
            audit,
            risk,
            initial_balance=self._initial_balance,
            fee_rate_percent=self._fee_rate,
            slippage_rate_percent=self._slippage_rate,
        )
        orchestrator = Orchestrator(
            state_machine=sm,
            event_bus=EventBus(),
            candle_store=store,
            decision_engine=DecisionEngine(),
            risk_manager=risk,
            paper_engine=paper,
            audit_service=audit,
            market_data_agent=MarketDataAgent(store, connection_status_fn=lambda: "REPLAY"),
            quant_agent=QuantAgent(store),
            trend_agent=TrendAgent(store),
            strategy_engine=self._strategy_engine or StrategyEngine(),
        )

        decisions = 0
        actionable = 0
        blocked_by_risk = 0
        for candle in ordered:
            decision = await orchestrator.on_candle_closed(candle)
            if decision is None:
                continue
            decisions += 1
            if decision.candidate_action.value in ("BUY", "SELL"):
                actionable += 1
                if decision.risk_status.value in ("BLOCKED", "KILL_SWITCH"):
                    blocked_by_risk += 1

        # Force-close any remaining open position at the last close (documented
        # simulation boundary; avoids phantom open PnL in metrics).
        last_close = ordered[-1].close
        for order_id in list(paper.open_orders.keys()):
            await paper.close_order(order_id, last_close, "BACKTEST_END")

        report = self._build_report(request, ordered, paper, decisions, actionable, blocked_by_risk)
        report = report.model_copy(
            update={"duration_ms": int((time.monotonic() - started) * 1000)}
        )
        self.reports.append(report)
        logger.info(
            f"Backtest completed: {report.total_trades} trades",
            event_type="BACKTEST_COMPLETED",
            correlation_id=report.backtest_id,
            metadata={"symbol": request.symbol, "net_pnl": report.net_pnl},
        )
        return report

    def _build_report(
        self,
        request: BacktestRequest,
        candles: list[Candle],
        paper: PaperTradingEngine,
        decisions: int,
        actionable: int,
        blocked_by_risk: int,
    ) -> BacktestReport:
        closed = paper.closed_orders
        wins = [o for o in closed if (o.pnl or 0) > 0]
        losses = [o for o in closed if (o.pnl or 0) <= 0]
        gross_wins = sum(o.pnl or 0 for o in wins)
        gross_losses = abs(sum(o.pnl or 0 for o in losses))
        net_pnl = sum(o.pnl or 0 for o in closed)
        total = len(closed)

        # Max consecutive losses.
        max_consecutive = current = 0
        for order in closed:
            if (order.pnl or 0) <= 0:
                current += 1
                max_consecutive = max(max_consecutive, current)
            else:
                current = 0

        win_rate = len(wins) / total * 100 if total else 0.0
        avg_win = gross_wins / len(wins) if wins else 0.0
        avg_loss = gross_losses / len(losses) if losses else 0.0
        expectancy = (
            (win_rate / 100) * avg_win - (1 - win_rate / 100) * avg_loss if total else 0.0
        )
        perf = paper.performance()

        return BacktestReport(
            backtest_id=str(uuid4()),
            symbol=request.symbol,
            timeframe=request.timeframe,
            start_date=candles[0].closed_at.date().isoformat(),
            end_date=candles[-1].closed_at.date().isoformat(),
            candles_processed=len(candles),
            decisions=decisions,
            actionable_decisions=actionable,
            blocked_by_risk=blocked_by_risk,
            total_trades=total,
            win_rate=round(win_rate, 2),
            loss_rate=round(100 - win_rate, 2) if total else 0.0,
            profit_factor=round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
            expectancy=round(expectancy, 4),
            max_drawdown=round(perf.max_drawdown_percent, 4),
            max_consecutive_losses=max_consecutive,
            avg_win=round(avg_win, 4),
            avg_loss=round(avg_loss, 4),
            net_pnl=round(net_pnl, 4),
            net_pnl_percent=round(net_pnl / self._initial_balance * 100, 4),
            fees=round(perf.fees_total, 4),
            slippage=round(perf.slippage_total, 4),
            final_balance=round(paper.balance, 2),
            equity_curve=[p.model_dump() for p in paper.equity_curve],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

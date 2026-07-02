"""Shared fixtures for the Phase 1 test suite (docs/22-testing-strategy.md)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.audit.service import AuditService
from app.core.event_bus import EventBus
from app.core.state_machine import SystemState, SystemStateMachine
from app.market_data.store import CandleStore
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import CandidateAction, Exchange
from app.schemas.decisions import Decision
from app.schemas.market import Candle
from app.schemas.risk import RiskLimits


def make_candle(
    close: float = 100.0,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
    closed_at: datetime | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "15m",
) -> Candle:
    open_ = open_ if open_ is not None else close * 0.999
    high = high if high is not None else max(open_, close) * 1.001
    low = low if low is not None else min(open_, close) * 0.999
    return Candle(
        exchange=Exchange.BINANCE,
        symbol=symbol,
        timeframe=timeframe,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        closed_at=closed_at or datetime.now(timezone.utc),
    )


def make_series(
    closes: list[float], *, symbol: str = "BTCUSDT", timeframe: str = "15m", volume: float = 100.0
) -> list[Candle]:
    """Ordered candle series ending now."""
    now = datetime.now(timezone.utc)
    step = timedelta(minutes=15)
    start = now - step * (len(closes) - 1)
    return [
        make_candle(c, volume=volume, closed_at=start + step * i, symbol=symbol, timeframe=timeframe)
        for i, c in enumerate(closes)
    ]


def make_decision(
    action: CandidateAction = CandidateAction.BUY, confidence: int = 80
) -> Decision:
    return Decision(
        correlation_id=str(uuid4()),
        symbol="BTCUSDT",
        timeframe="15m",
        candidate_action=action,
        confidence=confidence,
        agent_summary=[{"name": "QuantAgent", "signal": action.value}],
    )


@pytest.fixture
def state_machine() -> SystemStateMachine:
    return SystemStateMachine()


@pytest.fixture
async def paper_state_machine() -> SystemStateMachine:
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="test", actor="test")
    await sm.transition(SystemState.PAPER, reason="test", actor="test")
    return sm


@pytest.fixture
def audit_service() -> AuditService:
    return AuditService()


@pytest.fixture
async def risk_manager(paper_state_machine, audit_service) -> RiskManager:
    return RiskManager(
        RiskLimits(), paper_state_machine, audit_service, initial_balance=10_000.0
    )


@pytest.fixture
async def paper_engine(risk_manager, audit_service) -> PaperTradingEngine:
    return PaperTradingEngine(audit_service, risk_manager, initial_balance=10_000.0)


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture
def candle_store() -> CandleStore:
    return CandleStore()

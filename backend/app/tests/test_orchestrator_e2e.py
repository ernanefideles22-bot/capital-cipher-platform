"""End-to-end orchestration tests (docs/22 E2E flow, docs/04)."""

from __future__ import annotations

from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.trend import TrendAgent
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.service import Orchestrator
from app.tests.conftest import make_series


def build_orchestrator(paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service):
    return Orchestrator(
        state_machine=paper_state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        decision_engine=DecisionEngine(),
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        audit_service=audit_service,
        market_data_agent=MarketDataAgent(candle_store, connection_status_fn=lambda: "CONNECTED"),
        quant_agent=QuantAgent(candle_store),
        trend_agent=TrendAgent(candle_store),
    )


async def test_full_chain_bull_market(
    paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
):
    """Candle -> agents -> decision -> risk -> (maybe) paper order -> audit."""
    orchestrator = build_orchestrator(
        paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
    )
    # Steady uptrend to trigger bullish alignment.
    candles = make_series([100 * (1.004 ** i) for i in range(120)])
    decision = None
    for candle in candles:
        decision = await orchestrator.on_candle_closed(candle)
    assert decision is not None
    # Decision chain must be auditable via correlation_id (docs/10).
    chain = audit_service.query(correlation_id=decision.correlation_id)
    assert any(r["audit_type"] == "DECISION_CANDIDATE" for r in chain)
    if decision.candidate_action.value in ("BUY", "SELL"):
        assert any(r["audit_type"] == "RISK_CHECK" for r in chain)
        if decision.risk_status.value in ("APPROVED", "REDUCED"):
            assert any(r["audit_type"] == "PAPER_ORDER_CREATED" for r in chain)
            assert len(paper_engine.open_orders) >= 1


async def test_no_decision_when_system_offline(
    state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
):
    orchestrator = Orchestrator(
        state_machine=state_machine,  # OFFLINE
        event_bus=event_bus,
        candle_store=candle_store,
        decision_engine=DecisionEngine(),
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        audit_service=audit_service,
        market_data_agent=MarketDataAgent(candle_store),
        quant_agent=QuantAgent(candle_store),
        trend_agent=TrendAgent(candle_store),
    )
    candles = make_series([100.0 + i for i in range(5)])
    for candle in candles:
        assert await orchestrator.on_candle_closed(candle) is None


async def test_audit_failure_blocks_cycle(
    paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
):
    orchestrator = build_orchestrator(
        paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
    )
    candles = make_series([100 * (1.004 ** i) for i in range(120)])
    audit_service.fail_mode = True
    decision = None
    for candle in candles:
        decision = await orchestrator.on_candle_closed(candle)
    # Fail safe: audit broken -> no decision advances, no paper orders.
    assert decision is None
    assert len(paper_engine.open_orders) == 0


async def test_insufficient_data_never_creates_orders(
    paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
):
    orchestrator = build_orchestrator(
        paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
    )
    candles = make_series([100.0, 100.5, 101.0])
    for candle in candles:
        decision = await orchestrator.on_candle_closed(candle)
    assert len(paper_engine.open_orders) == 0

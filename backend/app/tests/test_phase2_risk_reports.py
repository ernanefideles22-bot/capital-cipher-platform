"""Phase 2: total drawdown gate, daily reset, per-symbol reports, ranking."""

from __future__ import annotations

from app.orchestrator.ranking import AgentRankingService
from app.schemas.common import RiskStatus
from app.tests.conftest import make_decision


async def test_total_drawdown_blocks(risk_manager):
    risk_manager.update_equity(10_000.0)
    risk_manager.update_equity(8_900.0)  # 11% below peak
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.BLOCKED
    assert "total drawdown" in check.reason.lower()


async def test_total_drawdown_below_limit_allows(risk_manager):
    risk_manager.update_equity(10_000.0)
    risk_manager.update_equity(9_500.0)  # 5% below peak: allowed (< 10%)
    check = await risk_manager.check(make_decision(), entry_price=100.0, atr=1.0)
    assert check.risk_status == RiskStatus.APPROVED


async def test_strategy_override_only_tightens(risk_manager):
    """Strategy asking for MORE risk than docs/06 must be capped."""
    check = await risk_manager.check(
        make_decision(),
        entry_price=100.0,
        atr=1.0,
        risk_per_trade_percent_override=5.0,  # aggressive ask
    )
    assert check.risk_status == RiskStatus.APPROVED
    assert check.risk_percent == risk_manager.limits.risk_per_trade_percent  # capped at 1.0


async def test_strategy_rr_override_raises_target(risk_manager):
    check = await risk_manager.check(
        make_decision(), entry_price=100.0, atr=1.0, min_risk_reward_override=2.2
    )
    assert check.risk_status == RiskStatus.APPROVED
    assert check.risk_reward >= 2.2


async def test_daily_reset(risk_manager):
    risk_manager.state.daily_pnl_percent = -4.9
    risk_manager.reset_daily()
    assert risk_manager.state.daily_pnl_percent == 0.0


async def test_performance_by_symbol(paper_engine, risk_manager):
    from app.tests.conftest import make_decision as md

    for symbol in ("BTCUSDT", "ETHUSDT"):
        decision = md()
        decision = decision.model_copy(update={"symbol": symbol})
        check = await risk_manager.check(decision, entry_price=100.0, atr=1.0)
        order = await paper_engine.create_order(decision, check, current_price=100.0)
        await paper_engine.close_order(order.paper_order_id, order.take_profit, "TAKE_PROFIT")
    breakdown = paper_engine.performance_by("symbol")
    assert {b.key for b in breakdown} == {"BTCUSDT", "ETHUSDT"}
    assert all(b.trades == 1 and b.net_pnl > 0 for b in breakdown)


async def test_equity_curve_tracks_closes(paper_engine, risk_manager):
    decision = make_decision()
    check = await risk_manager.check(decision, entry_price=100.0, atr=1.0)
    order = await paper_engine.create_order(decision, check, current_price=100.0)
    await paper_engine.close_order(order.paper_order_id, order.take_profit, "TAKE_PROFIT")
    assert len(paper_engine.equity_curve) == 2  # initial + one close
    assert paper_engine.equity_curve[-1].balance > paper_engine.equity_curve[0].balance


async def test_agent_ranking_is_report_only(
    paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
):
    from app.tests.test_orchestrator_e2e import build_orchestrator
    from app.tests.conftest import make_series

    orchestrator = build_orchestrator(
        paper_state_machine, event_bus, candle_store, risk_manager, paper_engine, audit_service
    )
    for candle in make_series([100 * (1.004 ** i) for i in range(120)]):
        await orchestrator.on_candle_closed(candle)
    service = AgentRankingService(orchestrator, paper_engine)
    report = service.report()
    names = {r["agent_name"] for r in report}
    assert {"MarketDataAgent", "QuantAgent", "TrendAgent"} <= names
    for row in report:
        assert row["sample_sufficient"] is False  # tiny sample: must not adjust weights
        assert "Report only" in row["note"]

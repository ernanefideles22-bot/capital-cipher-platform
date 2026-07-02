"""Application context: wires all components together (composition root)."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.trend import TrendAgent
from app.audit.service import AuditService
from app.backtesting.engine import BacktestingEngine
from app.core.config import Settings
from app.core.event_bus import EventBus
from app.core.state_machine import SystemStateMachine
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.store import CandleStore
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.service import Orchestrator
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.risk import RiskLimits


@dataclass
class AppContext:
    settings: Settings
    state_machine: SystemStateMachine
    event_bus: EventBus
    candle_store: CandleStore
    audit_service: AuditService
    risk_manager: RiskManager
    paper_engine: PaperTradingEngine
    orchestrator: Orchestrator
    backtesting_engine: BacktestingEngine = None  # type: ignore[assignment]
    database: Database | None = None
    repository: Repository | None = None
    market_connected: bool = False


def build_context(settings: Settings, *, with_database: bool = False) -> AppContext:
    state_machine = SystemStateMachine()
    event_bus = EventBus()
    candle_store = CandleStore()

    database: Database | None = None
    repository: Repository | None = None
    if with_database:
        database = Database(settings.database_url)
        repository = Repository(database)

    audit_service = AuditService(repository=repository)
    limits = RiskLimits(
        risk_per_trade_percent=settings.risk_per_trade_percent,
        max_daily_drawdown_percent=settings.max_daily_drawdown_percent,
        max_consecutive_losses=settings.max_consecutive_losses,
        max_open_positions=settings.max_open_positions,
        default_leverage=settings.default_leverage,
        max_leverage=settings.max_leverage_simulated,
        max_market_data_delay_ms=settings.max_market_data_delay_ms,
    )
    risk_manager = RiskManager(
        limits, state_machine, audit_service, initial_balance=settings.paper_initial_balance
    )
    paper_engine = PaperTradingEngine(
        audit_service,
        risk_manager,
        initial_balance=settings.paper_initial_balance,
        fee_rate_percent=settings.fee_rate_percent,
        slippage_rate_percent=settings.slippage_rate_percent,
        repository=repository,
    )
    context_holder: dict = {}
    market_data_agent = MarketDataAgent(
        candle_store, connection_status_fn=lambda: (
            "CONNECTED" if context_holder.get("ctx") and context_holder["ctx"].market_connected else "DISCONNECTED"
        )
    )
    quant_agent = QuantAgent(candle_store)
    trend_agent = TrendAgent(candle_store)
    decision_engine = DecisionEngine(
        minimum_candidate_confidence=settings.minimum_candidate_confidence
    )
    orchestrator = Orchestrator(
        state_machine=state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        decision_engine=decision_engine,
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        audit_service=audit_service,
        market_data_agent=market_data_agent,
        quant_agent=quant_agent,
        trend_agent=trend_agent,
        repository=repository,
        max_data_delay_ms=settings.max_market_data_delay_ms,
    )
    backtesting_engine = BacktestingEngine(
        limits=limits,
        initial_balance=settings.paper_initial_balance,
        fee_rate_percent=settings.fee_rate_percent,
        slippage_rate_percent=settings.slippage_rate_percent,
    )
    ctx = AppContext(
        settings=settings,
        state_machine=state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        audit_service=audit_service,
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        orchestrator=orchestrator,
        backtesting_engine=backtesting_engine,
        database=database,
        repository=repository,
    )
    context_holder["ctx"] = ctx
    return ctx

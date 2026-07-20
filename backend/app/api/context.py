"""Application context: wires all components together (composition root)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.trend import TrendAgent
from app.audit.service import AuditService
from app.backtesting.engine import BacktestingEngine
from app.core.config import Settings
from app.core.event_bus import EventBus
from app.core.outbox import OutboxDispatcher
from app.core.state_machine import SystemStateMachine
from app.core.transports.base import EventTransport
from app.core.transports.redis_streams import RedisStreamTransport
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.adapters.binance_rest import BinancePublicRestClient
from app.market_data.adapters.bybit_rest import BybitPublicRestClient
from app.market_data.adapters.public_rest import PublicMarketDataClient
from app.market_data.backfill import HistoricalBackfillService
from app.market_data.catalog import DataCatalog
from app.market_data.clock import ExchangeClockMonitor, ExchangeClockRegistry
from app.market_data.gaps import GapService
from app.market_data.store import CandleStore
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.service import Orchestrator
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import Exchange
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
    data_catalog: DataCatalog | None = None
    gap_service: GapService | None = None
    backfill_service: HistoricalBackfillService | None = None
    clock_registry: ExchangeClockRegistry | None = None
    clock_monitor: ExchangeClockMonitor | None = None
    public_market_clients: dict[Exchange, PublicMarketDataClient] | None = None
    event_transport: EventTransport | None = None
    outbox_dispatcher: OutboxDispatcher | None = None
    market_connected: bool = False


def build_context(settings: Settings, *, with_database: bool = False) -> AppContext:
    state_machine = SystemStateMachine()
    candle_store = CandleStore()

    database: Database | None = None
    repository: Repository | None = None
    if with_database:
        database = Database(settings.database_url)
        repository = Repository(database)
    data_catalog = DataCatalog(repository) if repository is not None else None
    gap_service = GapService(repository) if repository is not None else None
    clock_registry = ExchangeClockRegistry(
        max_age_seconds=settings.clock_observation_max_age_seconds
    )
    public_market_clients: dict[Exchange, PublicMarketDataClient] | None = None
    clock_monitor: ExchangeClockMonitor | None = None
    backfill_service: HistoricalBackfillService | None = None
    if repository is not None and data_catalog is not None and gap_service is not None:
        public_market_clients = {
            Exchange.BINANCE: BinancePublicRestClient(
                base_url=settings.binance_public_rest_url,
                timeout_seconds=settings.public_market_http_timeout_seconds,
            ),
            Exchange.BYBIT: BybitPublicRestClient(
                base_url=settings.bybit_public_rest_url,
                timeout_seconds=settings.public_market_http_timeout_seconds,
            ),
        }
        clock_monitor = ExchangeClockMonitor(
            public_market_clients,
            clock_registry,
            repository,
            interval_seconds=settings.clock_probe_interval_seconds,
            warning_offset_ms=settings.clock_warning_offset_ms,
            unsafe_offset_ms=settings.clock_unsafe_offset_ms,
            warning_round_trip_ms=settings.clock_warning_round_trip_ms,
            unsafe_round_trip_ms=settings.clock_unsafe_round_trip_ms,
        )
        backfill_service = HistoricalBackfillService(
            repository=repository,
            clients=public_market_clients,
            clock_monitor=clock_monitor,
            clock_registry=clock_registry,
            gap_service=gap_service,
            data_catalog=data_catalog,
            max_candles=settings.historical_backfill_max_candles,
        )
    event_transport: EventTransport | None = None
    outbox_dispatcher: OutboxDispatcher | None = None
    publication_lock = asyncio.Lock()
    if settings.redis_url:
        if repository is None:
            raise ValueError("Redis Streams requires durable database journaling")
        event_transport = RedisStreamTransport(
            settings.redis_url,
            stream_prefix=settings.redis_stream_prefix,
            max_stream_length=settings.redis_stream_max_length,
            max_message_bytes=settings.broker_max_message_bytes,
        )
        outbox_dispatcher = OutboxDispatcher(
            repository,
            event_transport,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            publication_lock=publication_lock,
        )
    event_bus = EventBus(
        journal=repository.save_bus_message if repository is not None else None,
        transport=event_transport,
        transport_required=settings.event_broker_required,
        mark_published=(
            repository.mark_bus_message_published if repository is not None else None
        ),
        mark_failed=(
            repository.mark_bus_message_failed if repository is not None else None
        ),
        publication_lock=publication_lock,
    )

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
        clock_registry=clock_registry,
        require_trusted_clock=settings.require_trusted_market_clock,
        gap_service=gap_service,
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
        data_catalog=data_catalog,
        gap_service=gap_service,
        backfill_service=backfill_service,
        clock_registry=clock_registry,
        clock_monitor=clock_monitor,
        public_market_clients=public_market_clients,
        event_transport=event_transport,
        outbox_dispatcher=outbox_dispatcher,
    )
    context_holder["ctx"] = ctx
    return ctx

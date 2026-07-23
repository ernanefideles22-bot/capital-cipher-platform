"""Application context: wires all components together (composition root)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.agents.market_data import MarketDataAgent
from app.agents.evaluation import (
    AgentEvaluationService,
    SpecialistEvidenceService,
)
from app.agents.advanced_specialists import (
    build_advanced_shadow_specialists,
)
from app.agents.quant import QuantAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime, AgentRuntimeWorker
from app.agents.specialists import build_shadow_specialists
from app.agents.month8_specialists import build_month8_shadow_specialists
from app.agents.month9_specialists import build_month9_shadow_specialists
from app.agents.month10_specialists import build_month10_shadow_specialists
from app.agents.month11_specialists import build_month11_shadow_specialists
from app.agents.trend import TrendAgent
from app.audit.service import AuditService
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import WalkForwardEngine
from app.core.config import Settings
from app.core.event_bus import EventBus
from app.core.outbox import OutboxDispatcher
from app.core.publication import PublicationCoordinator
from app.core.state_machine import SystemStateMachine
from app.core.transports.base import EventTransport
from app.core.transports.redis_streams import RedisStreamTransport
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.execution.adapters.binance_testnet import (
    BinanceTestnetExecutionAdapter,
)
from app.execution.adapters.bybit_testnet import (
    BybitTestnetExecutionAdapter,
)
from app.execution.adapters.paper import PaperExecutionAdapter
from app.execution.credentials import EnvironmentTestnetCredentialProvider
from app.market_data.adapters.binance_rest import BinancePublicRestClient
from app.market_data.adapters.bybit_rest import BybitPublicRestClient
from app.market_data.adapters.public_rest import PublicMarketDataClient
from app.market_data.backfill import HistoricalBackfillService
from app.market_data.backfill_worker import HistoricalBackfillWorker
from app.market_data.catalog import DataCatalog
from app.market_data.clock import ExchangeClockMonitor, ExchangeClockRegistry
from app.market_data.data_lake import (
    LocalContentAddressedBlobStore,
    RawDataLake,
)
from app.market_data.gaps import GapService
from app.market_data.store import CandleStore
from app.orchestrator.decision_engine import DecisionEngine
from app.orchestrator.portfolio_consensus import (
    ConsensusExperimentService,
    DriftMonitor,
    PortfolioConstructionService,
    WeightedConsensusService,
)
from app.orchestrator.service import Orchestrator
from app.oms.reconciliation import ReconciliationService
from app.oms.service import OMSService
from app.operations.service import OperationsService
from app.paper_trading.engine import PaperTradingEngine
from app.release_readiness.service import ReleaseReadinessService
from app.risk.manager import RiskManager
from app.shadow_validation.service import ShadowValidationService
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment
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
    oms_service: OMSService
    reconciliation_service: ReconciliationService
    execution_adapters: dict[
        tuple[Exchange, ExecutionEnvironment],
        ExchangeExecutionAdapter,
    ]
    orchestrator: Orchestrator
    backtesting_engine: BacktestingEngine = None  # type: ignore[assignment]
    walk_forward_engine: WalkForwardEngine = None  # type: ignore[assignment]
    database: Database | None = None
    repository: Repository | None = None
    data_catalog: DataCatalog | None = None
    gap_service: GapService | None = None
    backfill_service: HistoricalBackfillService | None = None
    backfill_worker: HistoricalBackfillWorker | None = None
    raw_data_lake: RawDataLake | None = None
    raw_blob_store: LocalContentAddressedBlobStore | None = None
    clock_registry: ExchangeClockRegistry | None = None
    clock_monitor: ExchangeClockMonitor | None = None
    public_market_clients: dict[Exchange, PublicMarketDataClient] | None = None
    event_transport: EventTransport | None = None
    outbox_dispatcher: OutboxDispatcher | None = None
    agent_registry: AgentRegistry | None = None
    agent_runtime: AgentRuntime | None = None
    agent_runtime_worker: AgentRuntimeWorker | None = None
    specialist_evidence_service: SpecialistEvidenceService | None = None
    agent_evaluation_service: AgentEvaluationService | None = None
    consensus_experiment_service: ConsensusExperimentService | None = None
    drift_monitor: DriftMonitor | None = None
    weighted_consensus_service: WeightedConsensusService | None = None
    portfolio_construction_service: PortfolioConstructionService | None = None
    operations_service: OperationsService | None = None
    shadow_validation_service: ShadowValidationService | None = None
    release_readiness_service: ReleaseReadinessService | None = None
    market_connected: bool = False


def build_context(settings: Settings, *, with_database: bool = False) -> AppContext:
    state_machine = SystemStateMachine()
    candle_store = CandleStore()

    database: Database | None = None
    repository: Repository | None = None
    if with_database:
        database = Database(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
            pool_recycle_seconds=settings.database_pool_recycle_seconds,
        )
        repository = Repository(database)
    data_catalog = DataCatalog(repository) if repository is not None else None
    gap_service = GapService(repository) if repository is not None else None
    clock_registry = ExchangeClockRegistry(
        max_age_seconds=settings.clock_observation_max_age_seconds
    )
    public_market_clients: dict[Exchange, PublicMarketDataClient] | None = None
    clock_monitor: ExchangeClockMonitor | None = None
    backfill_service: HistoricalBackfillService | None = None
    backfill_worker: HistoricalBackfillWorker | None = None
    raw_data_lake: RawDataLake | None = None
    raw_blob_store: LocalContentAddressedBlobStore | None = None
    if repository is not None and data_catalog is not None and gap_service is not None:
        raw_blob_store = LocalContentAddressedBlobStore(settings.data_lake_root)
        raw_data_lake = RawDataLake(repository, raw_blob_store)
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
            raw_data_lake=raw_data_lake,
            max_candles=settings.historical_backfill_max_candles,
        )
        backfill_worker = HistoricalBackfillWorker(
            repository=repository,
            service=backfill_service,
            poll_interval_seconds=(
                settings.backfill_worker_poll_interval_seconds
            ),
            lease_seconds=settings.backfill_lease_seconds,
            retry_base_seconds=settings.backfill_retry_base_seconds,
            retry_max_seconds=settings.backfill_retry_max_seconds,
        )
    event_transport: EventTransport | None = None
    outbox_dispatcher: OutboxDispatcher | None = None
    publication_coordinator = PublicationCoordinator(
        max_concurrency=settings.event_publication_max_concurrency
    )
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
            publication_coordinator=publication_coordinator,
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
        publication_coordinator=publication_coordinator,
    )

    audit_service = AuditService(repository=repository)
    limits = RiskLimits(
        risk_per_trade_percent=settings.risk_per_trade_percent,
        max_daily_drawdown_percent=settings.max_daily_drawdown_percent,
        max_total_drawdown_percent=settings.max_total_drawdown_percent,
        max_consecutive_losses=settings.max_consecutive_losses,
        max_open_positions=settings.max_open_positions,
        default_leverage=settings.default_leverage,
        max_leverage=settings.max_leverage_simulated,
        max_market_data_delay_ms=settings.max_market_data_delay_ms,
        max_gross_exposure_percent=settings.max_gross_exposure_percent,
        max_net_exposure_percent=settings.max_net_exposure_percent,
        max_symbol_exposure_percent=settings.max_symbol_exposure_percent,
        max_strategy_exposure_percent=settings.max_strategy_exposure_percent,
        max_single_position_percent=settings.max_single_position_percent,
        max_symbol_concentration_percent=(
            settings.max_symbol_concentration_percent
        ),
        max_portfolio_var_percent=settings.max_portfolio_var_percent,
        var_confidence=settings.var_confidence,
        var_lookback=settings.var_lookback,
        var_min_observations=settings.var_min_observations,
        fallback_volatility_percent=settings.fallback_volatility_percent,
        approval_ttl_seconds=settings.risk_approval_ttl_seconds,
        max_entry_deviation_bps=settings.max_entry_deviation_bps,
    )
    risk_manager = RiskManager(
        limits,
        state_machine,
        audit_service,
        initial_balance=settings.paper_initial_balance,
        repository=repository,
        candle_store=candle_store,
    )
    paper_engine = PaperTradingEngine(
        audit_service,
        risk_manager,
        initial_balance=settings.paper_initial_balance,
        fee_rate_percent=settings.fee_rate_percent,
        slippage_rate_percent=settings.slippage_rate_percent,
        repository=repository,
    )
    target_environment = ExecutionEnvironment(
        settings.oms_execution_environment
    )
    target_exchange = (
        Exchange.BINANCE
        if target_environment == ExecutionEnvironment.PAPER
        else Exchange(settings.oms_testnet_exchange)
    )
    execution_adapters: dict[
        tuple[Exchange, ExecutionEnvironment],
        ExchangeExecutionAdapter,
    ] = {
        (
            Exchange.BINANCE,
            ExecutionEnvironment.PAPER,
        ): PaperExecutionAdapter(paper_engine)
    }
    if target_environment == ExecutionEnvironment.TESTNET:
        if repository is None:
            raise ValueError("TESTNET OMS requires with_database=True")
        if not settings.database_url.startswith(
            ("postgresql://", "postgresql+asyncpg://")
        ):
            raise ValueError(
                "TESTNET OMS requires PostgreSQL with Month 7 migrations"
            )
        credentials = EnvironmentTestnetCredentialProvider().load(
            target_exchange
        )
        if target_exchange == Exchange.BINANCE:
            testnet_adapter: ExchangeExecutionAdapter = (
                BinanceTestnetExecutionAdapter(
                    credentials,
                    base_url=settings.binance_testnet_rest_url,
                    timeout_seconds=settings.oms_http_timeout_seconds,
                    receive_window_ms=settings.oms_receive_window_ms,
                )
            )
        else:
            testnet_adapter = BybitTestnetExecutionAdapter(
                credentials,
                base_url=settings.bybit_testnet_rest_url,
                category=settings.bybit_testnet_category,
                timeout_seconds=settings.oms_http_timeout_seconds,
                receive_window_ms=settings.oms_receive_window_ms,
            )
        execution_adapters[
            (target_exchange, ExecutionEnvironment.TESTNET)
        ] = testnet_adapter
    oms_service = OMSService(
        target_environment=target_environment,
        target_exchange=target_exchange,
        paper_engine=paper_engine,
        risk_manager=risk_manager,
        audit_service=audit_service,
        adapters=execution_adapters,
        repository=repository,
        lease_seconds=settings.oms_command_lease_seconds,
        poll_interval_seconds=settings.oms_worker_poll_interval_seconds,
    )
    reconciliation_service = ReconciliationService(
        adapter=oms_service.adapter,
        risk_manager=risk_manager,
        audit_service=audit_service,
        repository=repository,
        halt_on_critical_drift=settings.oms_halt_on_critical_drift,
        interval_seconds=settings.oms_reconciliation_interval_seconds,
    )
    context_holder: dict = {}
    market_data_agent = MarketDataAgent(
        candle_store, connection_status_fn=lambda: (
            "CONNECTED" if context_holder.get("ctx") and context_holder["ctx"].market_connected else "DISCONNECTED"
        )
    )
    quant_agent = QuantAgent(candle_store)
    trend_agent = TrendAgent(candle_store)
    specialist_evidence_service = SpecialistEvidenceService(repository)
    agent_evaluation_service = AgentEvaluationService(repository)
    runtime_agents = [
        market_data_agent,
        quant_agent,
        trend_agent,
        *build_shadow_specialists(candle_store),
        *build_advanced_shadow_specialists(candle_store),
        *build_month8_shadow_specialists(
            candle_store,
            specialist_evidence_service,
        ),
        *build_month9_shadow_specialists(candle_store),
        *build_month10_shadow_specialists(candle_store),
        *build_month11_shadow_specialists(candle_store),
    ]
    for agent in runtime_agents:
        agent.timeout_ms = settings.agent_timeout_ms
        agent.max_attempts = settings.agent_max_attempts
    agent_registry = AgentRegistry(runtime_agents)
    agent_registry.validate_cohort(expected_count=300)
    operations_service = OperationsService(
        agent_registry,
        repository=repository,
        metric_capacity=settings.operations_metric_capacity,
        window_seconds=settings.operations_window_seconds,
        daily_budget_usd=settings.operations_daily_budget_usd,
        budget_warning_percent=(
            settings.operations_budget_warning_percent
        ),
        agent_execution_unit_cost_usd=(
            settings.agent_execution_unit_cost_usd
        ),
        agent_success_target=settings.agent_success_slo,
        agent_p95_latency_target_ms=(
            settings.agent_p95_latency_slo_ms
        ),
        orchestrator_success_target=settings.orchestrator_success_slo,
        orchestrator_p95_latency_target_ms=(
            settings.orchestrator_p95_latency_slo_ms
        ),
        recovery_successes_required=(
            settings.recovery_successes_required
        ),
    )
    agent_runtime = AgentRuntime(
        agent_registry,
        repository=repository,
        event_bus=event_bus,
        lease_seconds=settings.agent_lease_seconds,
        retry_base_seconds=settings.agent_retry_base_seconds,
        retry_max_seconds=settings.agent_retry_max_seconds,
        max_concurrency=settings.agent_max_concurrency,
    )
    agent_runtime_worker = AgentRuntimeWorker(
        agent_runtime,
        poll_interval_seconds=settings.agent_worker_poll_interval_seconds,
        lease_seconds=settings.agent_lease_seconds,
        max_concurrency=settings.agent_max_concurrency,
    )
    shadow_validation_service = ShadowValidationService(
        runtime=agent_runtime,
        candle_store=candle_store,
        reconciliation=reconciliation_service,
        risk_manager=risk_manager,
        oms_service=oms_service,
        paper_engine=paper_engine,
        repository=repository,
    )
    release_readiness_service = ReleaseReadinessService(repository)
    decision_engine = DecisionEngine(
        minimum_candidate_confidence=settings.minimum_candidate_confidence
    )
    consensus_experiment_service = ConsensusExperimentService(repository)
    drift_monitor = DriftMonitor(
        agent_evaluation_service,
        repository,
    )
    weighted_consensus_service = WeightedConsensusService(
        agent_evaluation_service,
        consensus_experiment_service,
        drift_monitor,
        repository,
    )
    portfolio_construction_service = PortfolioConstructionService(
        limits,
        risk_manager,
        repository,
        max_target_weight_percent=(
            settings.portfolio_max_target_weight_percent
        ),
    )
    orchestrator = Orchestrator(
        state_machine=state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        decision_engine=decision_engine,
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        oms_service=oms_service,
        audit_service=audit_service,
        market_data_agent=market_data_agent,
        quant_agent=quant_agent,
        trend_agent=trend_agent,
        agent_runtime=agent_runtime,
        repository=repository,
        max_data_delay_ms=settings.max_market_data_delay_ms,
        clock_registry=clock_registry,
        require_trusted_clock=settings.require_trusted_market_clock,
        gap_service=gap_service,
        agent_evaluation_service=agent_evaluation_service,
        weighted_consensus_service=weighted_consensus_service,
        portfolio_construction_service=portfolio_construction_service,
        operations_service=operations_service,
    )
    backtesting_engine = BacktestingEngine(
        limits=limits,
        initial_balance=settings.paper_initial_balance,
        fee_rate_percent=settings.fee_rate_percent,
        slippage_rate_percent=settings.slippage_rate_percent,
        half_spread_bps=settings.backtest_half_spread_bps,
        volume_impact_bps=settings.backtest_volume_impact_bps,
        funding_rate_bps_per_8h=(
            settings.backtest_funding_rate_bps_per_8h
        ),
    )
    walk_forward_engine = WalkForwardEngine(
        backtesting_engine,
        repository=repository,
    )
    ctx = AppContext(
        settings=settings,
        state_machine=state_machine,
        event_bus=event_bus,
        candle_store=candle_store,
        audit_service=audit_service,
        risk_manager=risk_manager,
        paper_engine=paper_engine,
        oms_service=oms_service,
        reconciliation_service=reconciliation_service,
        execution_adapters=execution_adapters,
        orchestrator=orchestrator,
        backtesting_engine=backtesting_engine,
        walk_forward_engine=walk_forward_engine,
        database=database,
        repository=repository,
        data_catalog=data_catalog,
        gap_service=gap_service,
        backfill_service=backfill_service,
        backfill_worker=backfill_worker,
        raw_data_lake=raw_data_lake,
        raw_blob_store=raw_blob_store,
        clock_registry=clock_registry,
        clock_monitor=clock_monitor,
        public_market_clients=public_market_clients,
        event_transport=event_transport,
        outbox_dispatcher=outbox_dispatcher,
        agent_registry=agent_registry,
        agent_runtime=agent_runtime,
        agent_runtime_worker=agent_runtime_worker,
        specialist_evidence_service=specialist_evidence_service,
        agent_evaluation_service=agent_evaluation_service,
        consensus_experiment_service=consensus_experiment_service,
        drift_monitor=drift_monitor,
        weighted_consensus_service=weighted_consensus_service,
        portfolio_construction_service=portfolio_construction_service,
        operations_service=operations_service,
        shadow_validation_service=shadow_validation_service,
        release_readiness_service=release_readiness_service,
    )
    context_holder["ctx"] = ctx
    return ctx

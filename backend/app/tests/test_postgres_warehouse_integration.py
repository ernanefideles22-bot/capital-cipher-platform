"""Real PostgreSQL warehouse integration test, enabled by POSTGRES_TEST_URL."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import SQLAlchemyError

from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.agents.specialists import MomentumAgent
from app.backtesting.engine import BacktestingEngine
from app.backtesting.walk_forward import WalkForwardEngine
from app.database.models import (
    AgentExecutionAttemptModel,
    INTERNAL_SCHEMA,
    OMSOrderEventModel,
    OrderApprovalModel,
    RiskEvaluationModel,
    WalkForwardExperimentModel,
)
from app.database.repositories.repository import Repository
from app.database.session import Database
from app.market_data.catalog import DataCatalog
from app.market_data.gaps import GapService
from app.market_data.store import CandleStore
from app.audit.service import AuditService
from app.core.state_machine import SystemState, SystemStateMachine
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.backfill import HistoricalBackfillJob
from app.schemas.backtest import WalkForwardProtocol, WalkForwardRequest
from app.schemas.data_lake import (
    BackfillQueueItem,
    BackfillRawPageLink,
    RawDataObject,
)
from app.schemas.common import Exchange
from app.schemas.oms import (
    ExecutionCommand,
    ExecutionCommandType,
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
)
from app.schemas.risk import ApprovalStatus, RiskLimits
from app.tests.conftest import make_decision, make_series


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_URL"),
    reason="POSTGRES_TEST_URL is not configured",
)
async def test_real_postgres_internal_warehouse_round_trip():
    import asyncpg

    migration_root = (
        Path(__file__).resolve().parents[3]
        / "supabase"
        / "migrations"
    )
    migration_connection = await asyncpg.connect(
        os.environ["POSTGRES_TEST_URL"].replace(
            "postgresql+asyncpg://",
            "postgresql://",
        )
    )
    try:
        for migration_path in sorted(migration_root.glob("*.sql")):
            await migration_connection.execute(
                migration_path.read_text(encoding="utf-8")
            )
    finally:
        await migration_connection.close()

    database = Database(os.environ["POSTGRES_TEST_URL"])
    await database.create_all()
    await database.verify_testnet_oms_schema()
    repository = Repository(database)
    candles = make_series([100.0, 101.0, 102.0])

    assert await repository.save_candles(candles) == 3
    manifest = await DataCatalog(repository).materialize_candle_dataset(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        clock_status="SYNCED",
    )
    loaded = await repository.load_dataset_manifest(manifest.dataset_hash)
    gaps = await GapService(repository).scan(
        exchange="BINANCE",
        symbol="BTCUSDT",
        timeframe="15m",
        start_at=candles[0].closed_at,
        end_at=candles[-1].closed_at,
    )
    jobs = []
    for identity in ("d" * 64, "e" * 64):
        job = HistoricalBackfillJob(
            job_id=identity,
            request_fingerprint=identity,
            exchange="BINANCE",
            symbol="BTCUSDT",
            timeframe="15m",
            start_at=candles[0].closed_at,
            end_at=candles[-1].closed_at,
            source="binance.public-rest",
        )
        await repository.submit_historical_backfill(
            job,
            BackfillQueueItem(
                queue_id=identity,
                job_id=identity,
                exchange="BINANCE",
                symbol="BTCUSDT",
                timeframe="15m",
                start_at=candles[0].closed_at,
                end_at=candles[-1].closed_at,
                max_candles=3,
            ),
        )
        jobs.append(job)
    loaded_job = await repository.load_historical_backfill_job(jobs[0].job_id)
    claims = await asyncio.gather(
        repository.claim_next_backfill(
            worker_id="postgres-worker-one",
            lease_seconds=60,
        ),
        repository.claim_next_backfill(
            worker_id="postgres-worker-two",
            lease_seconds=60,
        ),
    )
    raw_object = RawDataObject(
        object_hash="f" * 64,
        object_uri=(
            "lake://raw/binance.public-rest/2026/07/20/ff/"
            f"{'f' * 64}.json.gz"
        ),
        uncompressed_bytes=100,
        stored_bytes=80,
    )
    raw_link = BackfillRawPageLink(
        page_id="a" * 64,
        job_id=jobs[0].job_id,
        attempt_count=1,
        page_index=0,
        object_hash=raw_object.object_hash,
        source="binance.public-rest",
        endpoint="/api/v3/klines",
        request_params={"symbol": "BTCUSDT"},
        fetched_at=candles[-1].received_at,
    )
    await repository.save_backfill_raw_page(raw_object, raw_link)
    loaded_raw_pages = await repository.list_backfill_raw_pages(jobs[0].job_id)
    artifact_candles = make_series(
        [100.0 + index * 0.1 for index in range(30)]
    )
    artifact = await WalkForwardEngine(BacktestingEngine()).run(
        WalkForwardRequest(
            candidate_version="SCALP_15M_v1",
            protocol=WalkForwardProtocol(
                train_candles=10,
                validation_candles=10,
                test_candles=10,
                embargo_candles=0,
                max_folds=1,
            ),
        ),
        artifact_candles,
    )
    await repository.save_walk_forward_report(artifact)
    loaded_artifact = await repository.load_walk_forward_report(
        artifact.experiment_id
    )
    momentum_agent = MomentumAgent(CandleStore())
    runtime = AgentRuntime(
        AgentRegistry([momentum_agent]),
        repository=repository,
        retry_base_seconds=0,
        retry_max_seconds=0,
    )
    runtime_trace = await runtime.execute(
        AgentExecutionRequest(
            idempotency_key="postgres-agent-runtime",
            input=AgentInput(
                request_id="postgres-agent-runtime",
                correlation_id="postgres-agent-correlation",
                agent_name=momentum_agent.name,
                symbol="BTCUSDT",
                timeframe="15m",
            ),
        )
    )
    risk_state_machine = SystemStateMachine()
    await risk_state_machine.transition(
        SystemState.INITIALIZING,
        reason="postgres test",
        actor="test",
    )
    await risk_state_machine.transition(
        SystemState.PAPER,
        reason="postgres test",
        actor="test",
    )
    risk_audit = AuditService(repository=repository)
    risk_manager = RiskManager(
        RiskLimits(),
        risk_state_machine,
        risk_audit,
        repository=repository,
    )
    paper_engine = PaperTradingEngine(
        risk_audit,
        risk_manager,
        repository=repository,
    )
    risk_decision = make_decision()
    central_check = await risk_manager.check(
        risk_decision,
        entry_price=100,
        atr=1,
    )
    central_order = await paper_engine.create_order(
        risk_decision,
        central_check,
        current_price=100,
    )
    central_oms = await repository.load_oms_order(
        central_order.paper_order_id
    )
    testnet_decision = make_decision().model_copy(
        update={"symbol": "ETHUSDT"}
    )
    testnet_check = await risk_manager.check(
        testnet_decision,
        entry_price=100,
        atr=1,
    )
    testnet_order = OMSOrder(
        client_order_id=(
            f"cc-{testnet_check.approval_id[:32]}"
        ),
        decision_id=testnet_decision.decision_id,
        risk_check_id=testnet_check.risk_check_id,
        approval_id=testnet_check.approval_id,
        request_fingerprint=testnet_check.request_fingerprint,
        correlation_id=testnet_decision.correlation_id,
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
        symbol=testnet_decision.symbol,
        timeframe=testnet_decision.timeframe,
        strategy=testnet_decision.strategy,
        side=testnet_decision.candidate_action.value,
        quantity=testnet_check.position_size / 100,
        requested_notional=testnet_check.position_size,
        leverage=testnet_check.leverage,
        reference_price=100,
        status=OMSOrderStatus.PENDING_SUBMISSION,
    )
    testnet_command = ExecutionCommand(
        oms_order_id=testnet_order.oms_order_id,
        command_type=ExecutionCommandType.SUBMIT,
    )
    await risk_manager.consume_oms_approval(
        testnet_decision,
        testnet_check,
        testnet_order,
        testnet_command,
    )
    claimed_command, claimed_order = (
        await repository.claim_execution_command(
            worker_id="postgres-oms-worker",
            lease_seconds=30,
        )
    )
    transition_at = datetime.now(timezone.utc)
    quarantined = claimed_order.model_copy(
        update={
            "status": OMSOrderStatus.QUARANTINED,
            "state_version": claimed_order.state_version + 1,
            "updated_at": transition_at,
            "terminal_at": transition_at,
            "rejection_reason": "POSTGRES_TEST",
        }
    )
    await repository.finish_execution_command(
        command_id=claimed_command.command_id,
        worker_id="postgres-oms-worker",
        order=quarantined,
        event_type="POSTGRES_TEST_QUARANTINED",
        error_type="RiskError",
    )
    stored_testnet_order = await repository.load_oms_order(
        testnet_order.oms_order_id
    )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(WalkForwardExperimentModel)
                .where(
                    WalkForwardExperimentModel.experiment_id
                    == artifact.experiment_id
                )
                .values(symbol="ETHUSDT")
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(AgentExecutionAttemptModel)
                .where(
                    AgentExecutionAttemptModel.execution_id
                    == runtime_trace.job.execution_id
                )
                .values(worker_id="tampered-worker")
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(RiskEvaluationModel)
                .where(
                    RiskEvaluationModel.evaluation_id
                    == central_check.evaluation_id
                )
                .values(risk_status="BLOCKED")
            )
    with pytest.raises(SQLAlchemyError, match="append-only"):
        async with database.session() as session, session.begin():
            await session.execute(
                update(OMSOrderEventModel)
                .where(
                    OMSOrderEventModel.oms_order_id
                    == central_order.paper_order_id
                )
                .values(status="UNKNOWN")
            )

    async with database.engine.connect() as connection:
        tables = set(
            await connection.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema_name"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        immutable_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table = "
                    "'walk_forward_experiments'"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        row_security_enabled = await connection.scalar(
            text(
                "SELECT c.relrowsecurity "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname = 'walk_forward_experiments'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        function_is_security_definer = await connection.scalar(
            text(
                "SELECT p.prosecdef "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname = "
                "'reject_walk_forward_experiment_mutation'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        agent_immutable_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table IN "
                    "('agent_execution_attempts', 'agent_memory_entries')"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        agent_rls_count = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname IN "
                "('agent_execution_jobs', 'agent_execution_attempts', "
                "'agent_memory_entries') "
                "AND c.relrowsecurity"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        agent_function_is_security_definer = await connection.scalar(
            text(
                "SELECT p.prosecdef "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname = 'reject_agent_evidence_mutation'"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        central_risk_rls_count = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname IN "
                "('risk_evaluations', 'order_approvals', "
                "'risk_control_state', 'risk_control_events') "
                "AND c.relrowsecurity"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        approval_status = await connection.scalar(
            text(
                "SELECT status FROM capital_cipher.order_approvals "
                "WHERE approval_id = :approval_id"
            ),
            {"approval_id": central_check.approval_id},
        )
        central_risk_security_definers = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname IN "
                "('reject_central_risk_evidence_mutation', "
                "'guard_order_approval_transition', "
                "'guard_risk_control_transition') "
                "AND p.prosecdef"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        oms_rls_count = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname IN "
                "('oms_orders', 'oms_order_events', "
                "'execution_commands', 'execution_fills', "
                "'reconciliation_runs', 'reconciliation_mismatches', "
                "'venue_position_snapshots', 'venue_balance_snapshots') "
                "AND c.relrowsecurity"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        oms_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table IN "
                    "('oms_orders', 'oms_order_events', "
                    "'execution_commands', 'execution_fills', "
                    "'reconciliation_runs', 'reconciliation_mismatches', "
                    "'venue_position_snapshots', "
                    "'venue_balance_snapshots')"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        oms_security_definers = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname IN "
                "('reject_oms_evidence_mutation', "
                "'guard_oms_order_transition', "
                "'guard_execution_command_transition') "
                "AND p.prosecdef"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        specialist_evaluation_rls_count = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema_name "
                "AND c.relname IN "
                "('specialist_evidence', 'agent_forecasts', "
                "'agent_forecast_outcomes') "
                "AND c.relrowsecurity"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
        specialist_evaluation_triggers = set(
            await connection.scalars(
                text(
                    "SELECT trigger_name "
                    "FROM information_schema.triggers "
                    "WHERE trigger_schema = :schema_name "
                    "AND event_object_table IN "
                    "('specialist_evidence', 'agent_forecasts', "
                    "'agent_forecast_outcomes')"
                ),
                {"schema_name": INTERNAL_SCHEMA},
            )
        )
        specialist_evaluation_security_definers = await connection.scalar(
            text(
                "SELECT count(*) "
                "FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = :schema_name "
                "AND p.proname = "
                "'reject_specialist_evaluation_mutation' "
                "AND p.prosecdef"
            ),
            {"schema_name": INTERNAL_SCHEMA},
        )
    await database.dispose()

    assert loaded == manifest
    assert gaps == []
    assert loaded_job == jobs[0]
    assert loaded_artifact == artifact
    assert runtime_trace.job.status == "COMPLETED"
    assert len(runtime_trace.attempts) == 1
    assert len(runtime_trace.memory) == 3
    assert {claim.queue_id for claim in claims if claim is not None} == {
        jobs[0].job_id,
        jobs[1].job_id,
    }
    assert loaded_raw_pages == [raw_link]
    assert {
        "candle_observations",
        "dataset_manifests",
        "clock_observations",
        "market_data_gaps",
        "historical_backfill_jobs",
        "backfill_queue_items",
        "raw_data_objects",
        "backfill_raw_pages",
        "walk_forward_experiments",
        "agent_execution_jobs",
        "agent_execution_attempts",
        "agent_memory_entries",
        "risk_evaluations",
        "order_approvals",
        "risk_control_state",
        "risk_control_events",
        "oms_orders",
        "oms_order_events",
        "execution_commands",
        "execution_fills",
        "reconciliation_runs",
        "reconciliation_mismatches",
        "venue_position_snapshots",
        "venue_balance_snapshots",
        "specialist_evidence",
        "agent_forecasts",
        "agent_forecast_outcomes",
    } <= tables
    assert "trg_walk_forward_experiments_immutable" in immutable_triggers
    assert row_security_enabled is True
    assert function_is_security_definer is False
    assert agent_immutable_triggers == {
        "trg_agent_execution_attempts_immutable",
        "trg_agent_memory_entries_immutable",
    }
    assert agent_rls_count == 3
    assert agent_function_is_security_definer is False
    assert central_order.approval_id == central_check.approval_id
    assert central_oms.venue_order_id == central_order.paper_order_id
    assert stored_testnet_order.status == OMSOrderStatus.QUARANTINED
    assert approval_status == ApprovalStatus.CONSUMED.value
    assert central_risk_rls_count == 4
    assert central_risk_security_definers == 0
    assert oms_rls_count == 8
    assert oms_security_definers == 0
    assert specialist_evaluation_rls_count == 3
    assert specialist_evaluation_security_definers == 0
    assert specialist_evaluation_triggers == {
        "trg_specialist_evidence_immutable",
        "trg_agent_forecasts_immutable",
        "trg_agent_forecast_outcomes_immutable",
    }
    assert {
        "trg_oms_orders_transition",
        "trg_oms_order_events_immutable",
        "trg_execution_commands_transition",
        "trg_execution_fills_immutable",
        "trg_reconciliation_runs_immutable",
        "trg_reconciliation_mismatches_immutable",
        "trg_venue_position_snapshots_immutable",
        "trg_venue_balance_snapshots_immutable",
    } <= oms_triggers

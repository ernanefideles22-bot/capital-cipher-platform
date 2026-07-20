"""Persistence repository for the decision chain (docs/12).

Critical rule: if a decision or risk check cannot be recorded, the operation
must not advance (enforced by callers via raised DatabaseError).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.errors import DatabaseError, ValidationError
from app.backtesting.artifacts import walk_forward_artifact_hash
from app.database.models import (
    AgentExecutionAttemptModel,
    AgentExecutionJobModel,
    AgentForecastModel,
    AgentForecastOutcomeModel,
    AgentMemoryEntryModel,
    AgentOutputModel,
    AuditLogModel,
    BackfillQueueItemModel,
    BackfillRawPageModel,
    CandleObservationModel,
    ClockObservationModel,
    DecisionModel,
    DatasetManifestModel,
    DriftObservationModel,
    ExecutionCommandModel,
    ExecutionFillModel,
    EventJournalModel,
    EventOutboxModel,
    HistoricalBackfillJobModel,
    MarketDataGapModel,
    OMSOrderEventModel,
    OMSOrderModel,
    OperationalAlertEventModel,
    OperationalMetricSnapshotModel,
    PaperOrderModel,
    PortfolioProposalModel,
    RawMarketEventModel,
    RawDataObjectModel,
    ReconciliationMismatchModel,
    ReconciliationRunModel,
    ReplayCheckpointModel,
    RiskControlEventModel,
    RiskControlStateModel,
    RiskEvaluationModel,
    RiskCheckModel,
    OrderApprovalModel,
    SystemEventModel,
    SpecialistEvidenceModel,
    SLOEvaluationModel,
    ConsensusExperimentEventModel,
    ConsensusExperimentModel,
    VenueBalanceSnapshotModel,
    VenuePositionSnapshotModel,
    WalkForwardExperimentModel,
    WeightedConsensusModel,
    CostUsageRecordModel,
    ResilienceTestRunModel,
)
from app.database.session import Database
from app.market_data.identity import candle_event_id
from app.schemas.agents import (
    AgentExecutionAttempt,
    AgentExecutionJob,
    AgentExecutionTrace,
    AgentInput,
    AgentMemoryEntry,
    AgentOutput,
)
from app.schemas.backfill import HistoricalBackfillJob, MarketDataGap
from app.schemas.backtest import (
    WalkForwardArtifactMetadata,
    WalkForwardReport,
)
from app.schemas.common import AgentStatus, Exchange, OrderSide, PaperOrderStatus
from app.schemas.data_lake import (
    BackfillQueueItem,
    BackfillRawPageLink,
    RawDataObject,
)
from app.schemas.data_catalog import CandleDatasetManifest, ClockObservation
from app.schemas.decisions import Decision
from app.schemas.events import BusMessage
from app.schemas.replay import ReplayCheckpoint
from app.schemas.market import Candle, DataQualityReport, RawMarketEvent
from app.schemas.paper import PaperOrder
from app.schemas.oms import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionCommandType,
    ExecutionEnvironment,
    ExecutionFill,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    OMSTimeInForce,
    ReconciliationMismatch,
    ReconciliationMismatchType,
    ReconciliationRun,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    TERMINAL_OMS_STATUSES,
    VenueBalanceSnapshot,
    VenueOrderSnapshot,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)
from app.schemas.risk import (
    ApprovalStatus,
    OrderApproval,
    PositionExposure,
    RiskCheck,
    RiskControlState,
)
from app.schemas.specialist_evaluation import (
    AgentForecast,
    AgentForecastOutcome,
    SpecialistEvidence,
)
from app.schemas.portfolio_consensus import (
    ConsensusExperiment,
    ConsensusExperimentEvent,
    DriftObservation,
    PortfolioProposal,
    WeightedConsensus,
)
from app.schemas.operations import (
    CostUsageRecord,
    OperationalAlertEvent,
    OperationalMetricSnapshot,
    ResilienceTestRun,
    SLOEvaluation,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _position_snapshot_hash(rows: list[PaperOrderModel]) -> str:
    return _exposure_snapshot_hash(
        [
            PositionExposure(
                paper_order_id=row.id,
                symbol=row.symbol,
                timeframe=row.timeframe or "unknown",
                strategy=row.strategy,
                side=OrderSide(row.side),
                notional=float(row.position_size),
                leverage=float(row.leverage),
            )
            for row in rows
        ]
    )


def _exposure_snapshot_hash(positions: list[PositionExposure]) -> str:
    payload = {
        "positions": sorted(
            (
                {
                    "paper_order_id": position.paper_order_id,
                    "symbol": position.symbol,
                    "timeframe": position.timeframe,
                    "strategy": position.strategy,
                    "side": position.side.value,
                    "notional": round(position.notional, 8),
                    "leverage": round(position.leverage, 8),
                }
                for position in positions
            ),
            key=lambda item: item["paper_order_id"],
        )
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def _spot_base_asset(symbol: str) -> str | None:
    normalized = symbol.upper()
    for quote_asset in (
        "USDT",
        "USDC",
        "FDUSD",
        "TUSD",
        "BUSD",
        "BTC",
        "ETH",
        "BNB",
    ):
        if normalized.endswith(quote_asset):
            base_asset = normalized[: -len(quote_asset)]
            return base_asset or None
    return None


class Repository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def _durable_position_exposures(
        self,
        session,
        *,
        lock_paper: bool,
    ) -> list[PositionExposure]:
        paper_statement = select(PaperOrderModel).where(
            PaperOrderModel.status == PaperOrderStatus.FILLED.value
        )
        if lock_paper:
            paper_statement = paper_statement.with_for_update()
        paper_rows = list(await session.scalars(paper_statement))
        exposures = [
            PositionExposure(
                paper_order_id=row.id,
                symbol=row.symbol,
                timeframe=row.timeframe or "unknown",
                strategy=row.strategy,
                side=OrderSide(row.side),
                notional=float(row.position_size),
                leverage=float(row.leverage),
            )
            for row in paper_rows
        ]
        testnet_order_statement = select(OMSOrderModel).where(
            OMSOrderModel.environment
            == ExecutionEnvironment.TESTNET.value
        )
        if lock_paper:
            testnet_order_statement = (
                testnet_order_statement.with_for_update()
            )
        testnet_order_rows = list(
            await session.scalars(testnet_order_statement)
        )
        reserving_statuses = {
            OMSOrderStatus.CREATED.value,
            OMSOrderStatus.PENDING_SUBMISSION.value,
            OMSOrderStatus.SUBMITTED.value,
            OMSOrderStatus.PARTIALLY_FILLED.value,
            OMSOrderStatus.CANCEL_PENDING.value,
            OMSOrderStatus.UNKNOWN.value,
        }
        for row in testnet_order_rows:
            if row.status not in reserving_statuses:
                continue
            remaining_quantity = max(
                0.0,
                float(row.quantity)
                - float(row.cumulative_filled_quantity),
            )
            reserved_notional = (
                remaining_quantity * float(row.reference_price)
            )
            if reserved_notional <= 0:
                continue
            exposures.append(
                PositionExposure(
                    paper_order_id=(
                        f"oms-reservation:{row.oms_order_id}"
                    ),
                    symbol=row.symbol,
                    timeframe=row.timeframe,
                    strategy=row.strategy,
                    side=OrderSide(row.side),
                    notional=reserved_notional,
                    leverage=float(row.leverage),
                )
            )

        ranked_runs = (
            select(
                ReconciliationRunModel.run_id,
                ReconciliationRunModel.exchange,
                func.row_number()
                .over(
                    partition_by=ReconciliationRunModel.exchange,
                    order_by=ReconciliationRunModel.completed_at.desc(),
                )
                .label("venue_rank"),
            )
            .where(
                ReconciliationRunModel.environment
                == ExecutionEnvironment.TESTNET.value,
                ReconciliationRunModel.status
                != ReconciliationRunStatus.FAILED.value,
            )
            .subquery()
        )
        latest_run_rows = list(
            await session.execute(
                select(
                    ranked_runs.c.run_id,
                    ranked_runs.c.exchange,
                ).where(
                    ranked_runs.c.venue_rank == 1
                )
            )
        )
        latest_run_ids = [row.run_id for row in latest_run_rows]
        reconciled_exchanges = {
            row.exchange for row in latest_run_rows
        }
        reconciled_symbols: set[tuple[str, str]] = set()
        if latest_run_ids:
            position_rows = list(
                await session.scalars(
                    select(VenuePositionSnapshotModel).where(
                        VenuePositionSnapshotModel.run_id.in_(
                            latest_run_ids
                        )
                    )
                )
            )
            exposures.extend(
                PositionExposure(
                    paper_order_id=(
                        f"venue:{row.exchange}:{row.environment}:"
                        f"{row.symbol}:{row.side}"
                    ),
                    symbol=row.symbol,
                    timeframe="venue",
                    strategy="OMS_RECONCILED",
                    side=OrderSide(row.side),
                    notional=float(row.quantity)
                    * float(row.mark_price or row.entry_price or 0),
                    leverage=1.0,
                )
                for row in position_rows
                if float(row.quantity) > 0
                and float(row.mark_price or row.entry_price or 0) > 0
            )
            reconciled_symbols.update(
                (row.exchange, row.symbol) for row in position_rows
            )
            if Exchange.BYBIT.value in reconciled_exchanges:
                reconciled_symbols.update(
                    (row.exchange, row.symbol)
                    for row in testnet_order_rows
                    if row.exchange == Exchange.BYBIT.value
                )
            balance_rows = list(
                await session.scalars(
                    select(VenueBalanceSnapshotModel).where(
                        VenueBalanceSnapshotModel.run_id.in_(
                            latest_run_ids
                        ),
                        VenueBalanceSnapshotModel.exchange
                        == Exchange.BINANCE.value,
                    )
                )
            )
            latest_binance_orders: dict[str, OMSOrderModel] = {}
            for row in sorted(
                (
                    order
                    for order in testnet_order_rows
                    if order.exchange == Exchange.BINANCE.value
                ),
                key=lambda order: _as_utc(order.updated_at),
                reverse=True,
            ):
                latest_binance_orders.setdefault(row.symbol, row)
            balances_by_asset = {
                row.asset.upper(): row for row in balance_rows
            }
            for symbol, order in latest_binance_orders.items():
                base_asset = _spot_base_asset(symbol)
                if base_asset is None:
                    continue
                reconciled_symbols.add(
                    (Exchange.BINANCE.value, symbol)
                )
                balance = (
                    balances_by_asset.get(base_asset)
                    if base_asset is not None
                    else None
                )
                quantity = float(balance.equity) if balance else 0.0
                price = float(
                    order.average_fill_price or order.reference_price
                )
                if quantity <= 0 or price <= 0:
                    continue
                exposures.append(
                    PositionExposure(
                        paper_order_id=(
                            "venue:BINANCE:TESTNET:"
                            f"{symbol}:BUY"
                        ),
                        symbol=symbol,
                        timeframe="venue",
                        strategy="OMS_RECONCILED",
                        side=OrderSide.BUY,
                        notional=quantity * price,
                        leverage=1.0,
                    )
                )

        exposures.extend(
            PositionExposure(
                paper_order_id=f"oms:{row.oms_order_id}",
                symbol=row.symbol,
                timeframe=row.timeframe,
                strategy=row.strategy,
                side=OrderSide(row.side),
                notional=float(row.cumulative_filled_quantity)
                * float(row.average_fill_price or row.reference_price),
                leverage=float(row.leverage),
            )
            for row in testnet_order_rows
            if float(row.cumulative_filled_quantity) > 0
            and (row.exchange, row.symbol) not in reconciled_symbols
        )
        return exposures

    async def save_system_event(self, event: dict) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    SystemEventModel(
                        event_type=event["event_type"],
                        source=event["source"],
                        correlation_id=event["correlation_id"],
                        payload=event.get("payload", {}),
                        created_at=_now(),
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist system event: {exc}") from exc

    def _dialect_insert(self, model):
        dialect = self._db.engine.dialect.name
        if dialect == "postgresql":
            return postgresql_insert(model)
        if dialect == "sqlite":
            return sqlite_insert(model)
        return None

    @staticmethod
    def _walk_forward_report_from_row(
        row: WalkForwardExperimentModel,
    ) -> WalkForwardReport:
        expected_hash = walk_forward_artifact_hash(row.report_payload)
        report = WalkForwardReport.model_validate(row.report_payload)
        if (
            report.experiment_id != row.experiment_id
            or report.artifact_hash != row.artifact_hash
            or expected_hash != row.artifact_hash
            or report.dataset_hash != row.dataset_hash
            or report.dataset_id != row.dataset_id
            or report.candidate_version != row.candidate_version
            or report.promotion_status != row.promotion_status
            or report.schema_version != row.schema_version
            or row.artifact_version != "walk-forward-artifact-v1"
            or report.protocol.protocol_version != row.protocol_version
        ):
            raise ValueError(
                "Stored walk-forward artifact failed integrity validation"
            )
        return report

    @staticmethod
    def _agent_execution_job_from_row(
        row: AgentExecutionJobModel,
    ) -> AgentExecutionJob:
        return AgentExecutionJob(
            schema_version=row.schema_version,
            runtime_version=row.runtime_version,
            execution_id=row.execution_id,
            request_fingerprint=row.request_fingerprint,
            idempotency_key=row.idempotency_key,
            correlation_id=row.correlation_id,
            agent_name=row.agent_name,
            agent_version=row.agent_version,
            agent_definition_hash=row.agent_definition_hash,
            execution_mode=row.execution_mode,
            decision_role=row.decision_role,
            critical=row.critical,
            input=AgentInput.model_validate(row.input_payload),
            status=row.status,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            available_at=_as_utc(row.available_at),
            leased_by=row.leased_by,
            lease_expires_at=(
                _as_utc(row.lease_expires_at)
                if row.lease_expires_at
                else None
            ),
            last_error_code=row.last_error_code,
            output=(
                AgentOutput.model_validate(row.output_payload)
                if row.output_payload
                else None
            ),
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            completed_at=(
                _as_utc(row.completed_at)
                if row.completed_at
                else None
            ),
        )

    @staticmethod
    def _agent_attempt_from_row(
        row: AgentExecutionAttemptModel,
    ) -> AgentExecutionAttempt:
        return AgentExecutionAttempt(
            schema_version=row.schema_version,
            execution_id=row.execution_id,
            attempt_number=row.attempt_number,
            worker_id=row.worker_id,
            status=row.status,
            output=AgentOutput.model_validate(row.output_payload),
            retryable=row.retryable,
            started_at=_as_utc(row.started_at),
            completed_at=_as_utc(row.completed_at),
        )

    @staticmethod
    def _agent_memory_from_row(
        row: AgentMemoryEntryModel,
    ) -> AgentMemoryEntry:
        return AgentMemoryEntry(
            schema_version=row.schema_version,
            execution_id=row.execution_id,
            sequence=row.sequence,
            entry_type=row.entry_type,
            payload=row.payload,
            payload_hash=row.payload_hash,
            created_at=_as_utc(row.created_at),
        )

    async def save_bus_message(self, message: BusMessage):
        """Append a versioned event before any consumer handles it."""
        from app.core.journal import JournalWriteResult

        try:
            async with self._db.session() as session, session.begin():
                values = {
                    "message_id": message.message_id,
                    "event_id": message.event_id,
                    "correlation_id": message.correlation_id,
                    "topic": message.topic,
                    "event_type": message.event_type,
                    "source": message.source,
                    "schema_version": message.schema_version,
                    "payload": message.payload,
                    "created_at": message.timestamp,
                }
                insert_statement = self._dialect_insert(EventJournalModel)
                if insert_statement is not None:
                    statement = (
                        insert_statement.values(**values)
                        .on_conflict_do_nothing(index_elements=["event_id"])
                        .returning(EventJournalModel.event_id)
                    )
                    inserted_event_id = await session.scalar(statement)
                    if inserted_event_id is not None:
                        return JournalWriteResult(inserted=True, broker_published=False)
                else:
                    existing_id = await session.scalar(
                        select(EventJournalModel.event_id).where(
                            EventJournalModel.event_id == message.event_id
                        )
                    )
                    if existing_id is None:
                        session.add(EventJournalModel(**values))
                        return JournalWriteResult(inserted=True, broker_published=False)

                published_at = await session.scalar(
                    select(EventOutboxModel.published_at).where(
                        EventOutboxModel.event_id == message.event_id
                    )
                )
                return JournalWriteResult(
                    inserted=False,
                    broker_published=published_at is not None,
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to journal bus message: {exc}") from exc

    async def mark_bus_message_published(
        self, event_id: str, broker_message_id: str
    ) -> None:
        now = _now()
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(EventOutboxModel)
                values = {
                    "event_id": event_id,
                    "broker_message_id": broker_message_id,
                    "published_at": now,
                    "publish_attempts": 1,
                    "last_error_type": None,
                    "created_at": now,
                    "updated_at": now,
                }
                if insert_statement is not None:
                    await session.execute(
                        insert_statement.values(**values).on_conflict_do_update(
                            index_elements=["event_id"],
                            set_={
                                "broker_message_id": broker_message_id,
                                "published_at": now,
                                "publish_attempts": EventOutboxModel.publish_attempts + 1,
                                "last_error_type": None,
                                "updated_at": now,
                            },
                        )
                    )
                else:
                    row = await session.get(EventOutboxModel, event_id)
                    if row is None:
                        session.add(EventOutboxModel(**values))
                    else:
                        row.broker_message_id = broker_message_id
                        row.published_at = now
                        row.publish_attempts += 1
                        row.last_error_type = None
                        row.updated_at = now
        except Exception as exc:
            raise DatabaseError(f"Failed to mark bus message published: {exc}") from exc

    async def mark_bus_message_failed(self, event_id: str, error_type: str) -> None:
        now = _now()
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(EventOutboxModel)
                values = {
                    "event_id": event_id,
                    "broker_message_id": None,
                    "published_at": None,
                    "publish_attempts": 1,
                    "last_error_type": error_type,
                    "created_at": now,
                    "updated_at": now,
                }
                if insert_statement is not None:
                    await session.execute(
                        insert_statement.values(**values).on_conflict_do_update(
                            index_elements=["event_id"],
                            set_={
                                "publish_attempts": EventOutboxModel.publish_attempts + 1,
                                "last_error_type": error_type,
                                "updated_at": now,
                            },
                        )
                    )
                else:
                    row = await session.get(EventOutboxModel, event_id)
                    if row is None:
                        session.add(EventOutboxModel(**values))
                    else:
                        row.publish_attempts += 1
                        row.last_error_type = error_type
                        row.updated_at = now
        except Exception as exc:
            raise DatabaseError(f"Failed to mark bus message failed: {exc}") from exc

    async def list_pending_bus_messages(self, limit: int = 100) -> list[BusMessage]:
        async with self._db.session() as session:
            result = await session.execute(
                select(EventJournalModel)
                .outerjoin(
                    EventOutboxModel,
                    EventOutboxModel.event_id == EventJournalModel.event_id,
                )
                .where(EventOutboxModel.published_at.is_(None))
                .order_by(EventJournalModel.created_at, EventJournalModel.message_id)
                .limit(limit)
            )
            return [
                BusMessage(
                    message_id=row.message_id,
                    event_id=row.event_id,
                    correlation_id=row.correlation_id,
                    topic=row.topic,
                    event_type=row.event_type,
                    source=row.source,
                    timestamp=row.created_at,
                    schema_version=row.schema_version,
                    payload=row.payload,
                )
                for row in result.scalars()
            ]

    async def load_replay_checkpoint(
        self,
        replay_id: str,
        consumer_name: str,
        topic: str,
    ) -> ReplayCheckpoint | None:
        async with self._db.session() as session:
            row = await session.get(
                ReplayCheckpointModel,
                (replay_id, consumer_name, topic),
            )
            if row is None:
                return None
            return ReplayCheckpoint(
                replay_id=row.replay_id,
                consumer_name=row.consumer_name,
                topic=row.topic,
                schema_version=row.schema_version,
                dataset_hash=row.dataset_hash,
                next_offset=row.next_offset,
                last_event_id=row.last_event_id,
                events_processed=row.events_processed,
                status=row.status,
                updated_at=row.updated_at,
                completed_at=row.completed_at,
            )

    async def save_replay_checkpoint(self, checkpoint: ReplayCheckpoint) -> None:
        values = checkpoint.model_dump()
        insert_statement = self._dialect_insert(ReplayCheckpointModel)
        try:
            async with self._db.session() as session, session.begin():
                if insert_statement is not None:
                    statement = insert_statement.values(**values).on_conflict_do_update(
                        index_elements=["replay_id", "consumer_name", "topic"],
                        set_={
                            "schema_version": checkpoint.schema_version,
                            "dataset_hash": checkpoint.dataset_hash,
                            "next_offset": checkpoint.next_offset,
                            "last_event_id": checkpoint.last_event_id,
                            "events_processed": checkpoint.events_processed,
                            "status": checkpoint.status,
                            "updated_at": checkpoint.updated_at,
                            "completed_at": checkpoint.completed_at,
                        },
                    )
                    await session.execute(statement)
                else:
                    row = await session.get(
                        ReplayCheckpointModel,
                        (checkpoint.replay_id, checkpoint.consumer_name, checkpoint.topic),
                    )
                    if row is None:
                        session.add(ReplayCheckpointModel(**values))
                    else:
                        for key, value in values.items():
                            setattr(row, key, value)
        except Exception as exc:
            raise DatabaseError(f"Failed to save replay checkpoint: {exc}") from exc

    async def save_raw_market_event(self, event: RawMarketEvent) -> None:
        """Persist a public exchange payload once, before normalization."""
        try:
            async with self._db.session() as session, session.begin():
                existing = await session.get(RawMarketEventModel, event.event_id)
                if existing is None:
                    session.add(
                        RawMarketEventModel(
                            event_id=event.event_id,
                            schema_version=event.schema_version,
                            source=event.source,
                            exchange=event.exchange.value,
                            event_type=event.event_type,
                            symbol=event.symbol,
                            occurred_at=event.occurred_at,
                            received_at=event.received_at,
                            payload=event.payload,
                            payload_sha256=event.payload_sha256,
                        )
                    )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist raw market event: {exc}") from exc

    @staticmethod
    def _candle_values(
        candle: Candle,
        quality: DataQualityReport | None,
    ) -> dict:
        return {
            "candle_id": candle_event_id(candle),
            "schema_version": candle.schema_version,
            "exchange": candle.exchange.value,
            "symbol": candle.symbol,
            "timeframe": candle.timeframe,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
            "closed_at": candle.closed_at,
            "received_at": candle.received_at,
            "ingest_lag_ms": int(
                round(
                    (candle.received_at - candle.closed_at).total_seconds()
                    * 1_000
                )
            ),
            "quality_score": (
                quality.data_quality_score if quality is not None else None
            ),
            "quality_status": quality.status if quality is not None else "UNASSESSED",
            "quality_warnings": quality.warnings if quality is not None else [],
            "quality_errors": quality.errors if quality is not None else [],
            "recorded_at": _now(),
        }

    async def save_candle(
        self,
        candle: Candle,
        quality: DataQualityReport | None = None,
    ) -> bool:
        return await self.save_candles([candle], quality_reports=[quality]) == 1

    async def save_candles(
        self,
        candles: list[Candle],
        *,
        quality_reports: list[DataQualityReport | None] | None = None,
    ) -> int:
        """Batch append candles; exact duplicates are ignored idempotently."""
        if not candles:
            return 0
        if quality_reports is None:
            quality_reports = [None] * len(candles)
        if len(quality_reports) != len(candles):
            raise ValueError("quality_reports must match candles")
        values = [
            self._candle_values(candle, quality)
            for candle, quality in zip(candles, quality_reports)
        ]
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(CandleObservationModel)
                if insert_statement is not None:
                    result = await session.scalars(
                        insert_statement.values(values)
                        .on_conflict_do_nothing(index_elements=["candle_id"])
                        .returning(CandleObservationModel.candle_id)
                    )
                    return len(list(result))

                inserted = 0
                for item in values:
                    existing = await session.get(
                        CandleObservationModel,
                        item["candle_id"],
                    )
                    if existing is None:
                        session.add(CandleObservationModel(**item))
                        inserted += 1
                return inserted
        except Exception as exc:
            raise DatabaseError(f"Failed to persist candle batch: {exc}") from exc

    async def list_candles(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100_000,
    ) -> list[Candle]:
        if limit < 1 or limit > 1_000_000:
            raise ValueError("limit must be between 1 and 1000000")
        conditions = [
            CandleObservationModel.exchange == exchange.upper(),
            CandleObservationModel.symbol == symbol.upper(),
            CandleObservationModel.timeframe == timeframe,
        ]
        if start_at is not None:
            conditions.append(CandleObservationModel.closed_at >= start_at)
        if end_at is not None:
            conditions.append(CandleObservationModel.closed_at <= end_at)
        async with self._db.session() as session:
            rows = await session.scalars(
                select(CandleObservationModel)
                .where(*conditions)
                .order_by(CandleObservationModel.closed_at)
                .limit(limit)
            )
            return [
                Candle(
                    schema_version=row.schema_version,
                    exchange=row.exchange,
                    symbol=row.symbol,
                    timeframe=row.timeframe,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=float(row.volume),
                    closed_at=_as_utc(row.closed_at),
                    received_at=_as_utc(row.received_at),
                )
                for row in rows
            ]

    async def save_dataset_manifest(
        self,
        manifest: CandleDatasetManifest,
    ) -> bool:
        values = {
            "dataset_hash": manifest.dataset_hash,
            "dataset_id": manifest.dataset_id,
            "schema_version": manifest.schema_version,
            "candle_contract_version": manifest.candle_contract_version,
            "dataset_type": manifest.dataset_type,
            "exchange": manifest.exchange.value,
            "symbol": manifest.symbol,
            "timeframe": manifest.timeframe,
            "start_at": manifest.start_at,
            "end_at": manifest.end_at,
            "row_count": manifest.row_count,
            "selection": manifest.selection,
            "quality_summary": manifest.quality_summary,
            "clock_status": manifest.clock_status,
            "created_at": manifest.created_at,
        }
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(DatasetManifestModel)
                if insert_statement is not None:
                    inserted_hash = await session.scalar(
                        insert_statement.values(**values)
                        .on_conflict_do_nothing(index_elements=["dataset_hash"])
                        .returning(DatasetManifestModel.dataset_hash)
                    )
                    return inserted_hash is not None
                existing = await session.get(
                    DatasetManifestModel,
                    manifest.dataset_hash,
                )
                if existing is not None:
                    return False
                session.add(DatasetManifestModel(**values))
                return True
        except Exception as exc:
            raise DatabaseError(f"Failed to persist dataset manifest: {exc}") from exc

    async def load_dataset_manifest(
        self,
        dataset_hash: str,
    ) -> CandleDatasetManifest | None:
        async with self._db.session() as session:
            row = await session.get(DatasetManifestModel, dataset_hash)
            if row is None:
                return None
            return CandleDatasetManifest(
                dataset_hash=row.dataset_hash,
                dataset_id=row.dataset_id,
                schema_version=row.schema_version,
                candle_contract_version=row.candle_contract_version,
                dataset_type=row.dataset_type,
                exchange=row.exchange,
                symbol=row.symbol,
                timeframe=row.timeframe,
                start_at=_as_utc(row.start_at),
                end_at=_as_utc(row.end_at),
                row_count=row.row_count,
                selection=row.selection,
                quality_summary=row.quality_summary,
                clock_status=row.clock_status,
                created_at=_as_utc(row.created_at),
            )

    async def save_clock_observation(
        self,
        observation: ClockObservation,
    ) -> bool:
        values = observation.model_dump()
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(ClockObservationModel)
                if insert_statement is not None:
                    inserted_id = await session.scalar(
                        insert_statement.values(**values)
                        .on_conflict_do_nothing(index_elements=["observation_id"])
                        .returning(ClockObservationModel.observation_id)
                    )
                    return inserted_id is not None
                existing = await session.get(
                    ClockObservationModel,
                    observation.observation_id,
                )
                if existing is not None:
                    return False
                session.add(ClockObservationModel(**values))
                return True
        except Exception as exc:
            raise DatabaseError(f"Failed to persist clock observation: {exc}") from exc

    async def save_market_data_gaps(
        self,
        gaps: list[MarketDataGap],
    ) -> int:
        """Upsert deterministic gaps so repeat scans remain idempotent."""
        if not gaps:
            return 0
        values = [gap.model_dump() for gap in gaps]
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(MarketDataGapModel)
                if insert_statement is not None:
                    result = await session.scalars(
                        insert_statement.values(values)
                        .on_conflict_do_update(
                            index_elements=["gap_id"],
                            set_={
                                "missing_count": insert_statement.excluded.missing_count,
                                "status": insert_statement.excluded.status,
                                "detected_at": insert_statement.excluded.detected_at,
                                "resolved_at": insert_statement.excluded.resolved_at,
                                "backfill_job_id": func.coalesce(
                                    insert_statement.excluded.backfill_job_id,
                                    MarketDataGapModel.backfill_job_id,
                                ),
                            },
                        )
                        .returning(MarketDataGapModel.gap_id)
                    )
                    return len(list(result))

                for item in values:
                    row = await session.get(MarketDataGapModel, item["gap_id"])
                    if row is None:
                        session.add(MarketDataGapModel(**item))
                    else:
                        row.missing_count = item["missing_count"]
                        row.status = item["status"]
                        row.detected_at = item["detected_at"]
                        row.resolved_at = item["resolved_at"]
                        if item["backfill_job_id"] is not None:
                            row.backfill_job_id = item["backfill_job_id"]
                return len(values)
        except Exception as exc:
            raise DatabaseError(f"Failed to persist market-data gaps: {exc}") from exc

    async def resolve_market_data_gaps(
        self,
        *,
        exchange: str,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        unresolved_gap_ids: set[str],
        backfill_job_id: str | None = None,
    ) -> int:
        """Resolve known open gaps no longer present in a repeat scan."""
        conditions = [
            MarketDataGapModel.exchange == exchange.upper(),
            MarketDataGapModel.symbol == symbol.upper(),
            MarketDataGapModel.timeframe == timeframe,
            MarketDataGapModel.status.in_(("OPEN", "FILLING", "FAILED")),
            MarketDataGapModel.end_at >= start_at,
            MarketDataGapModel.start_at <= end_at,
        ]
        if unresolved_gap_ids:
            conditions.append(
                MarketDataGapModel.gap_id.not_in(sorted(unresolved_gap_ids))
            )
        values: dict = {
            "status": "RESOLVED",
            "resolved_at": _now(),
        }
        if backfill_job_id is not None:
            values["backfill_job_id"] = backfill_job_id
        try:
            async with self._db.session() as session, session.begin():
                result = await session.execute(
                    update(MarketDataGapModel)
                    .where(*conditions)
                    .values(**values)
                )
                return int(result.rowcount or 0)
        except Exception as exc:
            raise DatabaseError(f"Failed to resolve market-data gaps: {exc}") from exc

    async def list_market_data_gaps(
        self,
        *,
        exchange: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        status: str | None = None,
        backfill_job_id: str | None = None,
        limit: int = 500,
    ) -> list[MarketDataGap]:
        if limit < 1 or limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        conditions = []
        if exchange is not None:
            conditions.append(MarketDataGapModel.exchange == exchange.upper())
        if symbol is not None:
            conditions.append(MarketDataGapModel.symbol == symbol.upper())
        if timeframe is not None:
            conditions.append(MarketDataGapModel.timeframe == timeframe)
        if status is not None:
            conditions.append(MarketDataGapModel.status == status.upper())
        if backfill_job_id is not None:
            conditions.append(
                MarketDataGapModel.backfill_job_id == backfill_job_id
            )
        async with self._db.session() as session:
            rows = await session.scalars(
                select(MarketDataGapModel)
                .where(*conditions)
                .order_by(MarketDataGapModel.detected_at.desc())
                .limit(limit)
            )
            return [
                MarketDataGap(
                    schema_version=row.schema_version,
                    gap_id=row.gap_id,
                    exchange=row.exchange,
                    symbol=row.symbol,
                    timeframe=row.timeframe,
                    start_at=_as_utc(row.start_at),
                    end_at=_as_utc(row.end_at),
                    missing_count=row.missing_count,
                    status=row.status,
                    detected_at=_as_utc(row.detected_at),
                    resolved_at=(
                        _as_utc(row.resolved_at) if row.resolved_at else None
                    ),
                    backfill_job_id=row.backfill_job_id,
                )
                for row in rows
            ]

    async def save_historical_backfill_job(
        self,
        job: HistoricalBackfillJob,
    ) -> None:
        """Insert or update one idempotent historical import job."""
        values = job.model_dump()
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(HistoricalBackfillJobModel)
                if insert_statement is not None:
                    update_values = {
                        key: getattr(insert_statement.excluded, key)
                        for key in values
                        if key not in {"job_id", "request_fingerprint", "created_at"}
                    }
                    await session.execute(
                        insert_statement.values(**values).on_conflict_do_update(
                            index_elements=["job_id"],
                            set_=update_values,
                        )
                    )
                    return

                row = await session.get(HistoricalBackfillJobModel, job.job_id)
                if row is None:
                    session.add(HistoricalBackfillJobModel(**values))
                else:
                    for key, value in values.items():
                        if key not in {"job_id", "request_fingerprint", "created_at"}:
                            setattr(row, key, value)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist historical backfill job: {exc}"
            ) from exc

    async def load_historical_backfill_job(
        self,
        job_id: str,
    ) -> HistoricalBackfillJob | None:
        async with self._db.session() as session:
            row = await session.get(HistoricalBackfillJobModel, job_id)
            if row is None:
                return None
            return HistoricalBackfillJob(
                schema_version=row.schema_version,
                job_id=row.job_id,
                request_fingerprint=row.request_fingerprint,
                exchange=row.exchange,
                symbol=row.symbol,
                timeframe=row.timeframe,
                start_at=_as_utc(row.start_at),
                end_at=_as_utc(row.end_at),
                source=row.source,
                status=row.status,
                retrieved_count=row.retrieved_count,
                inserted_count=row.inserted_count,
                remaining_gap_count=row.remaining_gap_count,
                attempt_count=row.attempt_count,
                dataset_hash=row.dataset_hash,
                clock_observation_id=row.clock_observation_id,
                clock_status=row.clock_status,
                error_code=row.error_code,
                error_message=row.error_message,
                created_at=_as_utc(row.created_at),
                started_at=_as_utc(row.started_at) if row.started_at else None,
                completed_at=(
                    _as_utc(row.completed_at) if row.completed_at else None
                ),
                updated_at=_as_utc(row.updated_at),
            )

    @staticmethod
    def _queue_item_from_row(row: BackfillQueueItemModel) -> BackfillQueueItem:
        return BackfillQueueItem(
            schema_version=row.schema_version,
            queue_id=row.queue_id,
            job_id=row.job_id,
            exchange=row.exchange,
            symbol=row.symbol,
            timeframe=row.timeframe,
            start_at=_as_utc(row.start_at),
            end_at=_as_utc(row.end_at),
            max_candles=row.max_candles,
            status=row.status,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            available_at=_as_utc(row.available_at),
            leased_by=row.leased_by,
            lease_expires_at=(
                _as_utc(row.lease_expires_at)
                if row.lease_expires_at
                else None
            ),
            last_error_code=row.last_error_code,
            last_error_message=row.last_error_message,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
            completed_at=_as_utc(row.completed_at) if row.completed_at else None,
        )

    async def submit_historical_backfill(
        self,
        job: HistoricalBackfillJob,
        queue_item: BackfillQueueItem,
    ) -> bool:
        """Atomically persist a job summary and its durable queue item."""
        if job.job_id != queue_item.job_id:
            raise ValueError("job and queue item identities must match")
        job_values = job.model_dump()
        queue_values = queue_item.model_dump()
        try:
            async with self._db.session() as session, session.begin():
                job_insert = self._dialect_insert(HistoricalBackfillJobModel)
                queue_insert = self._dialect_insert(BackfillQueueItemModel)
                if job_insert is not None:
                    await session.execute(
                        job_insert.values(**job_values).on_conflict_do_nothing(
                            index_elements=["job_id"]
                        )
                    )
                else:
                    existing_job = await session.get(
                        HistoricalBackfillJobModel,
                        job.job_id,
                    )
                    if existing_job is None:
                        session.add(HistoricalBackfillJobModel(**job_values))
                        await session.flush()

                if queue_insert is not None:
                    await session.execute(
                        queue_insert.values(**queue_values).on_conflict_do_nothing(
                            index_elements=["queue_id"]
                        )
                    )
                else:
                    existing_queue = await session.get(
                        BackfillQueueItemModel,
                        queue_item.queue_id,
                    )
                    if existing_queue is None:
                        session.add(BackfillQueueItemModel(**queue_values))
                        await session.flush()

                queue_row = await session.get(
                    BackfillQueueItemModel,
                    queue_item.queue_id,
                    with_for_update=True,
                )
                if queue_row is None:
                    raise RuntimeError("backfill queue insert did not materialize")
                job_row = await session.get(
                    HistoricalBackfillJobModel,
                    job.job_id,
                    with_for_update=True,
                )
                if job_row is None:
                    raise RuntimeError("backfill job insert did not materialize")
                if job_row.status == "COMPLETED":
                    return False
                if queue_row.status == "LEASED":
                    return False
                if queue_row.status == "DEAD_LETTER":
                    queue_row.attempt_count = 0
                for key, value in job_values.items():
                    if key not in {
                        "job_id",
                        "request_fingerprint",
                        "created_at",
                        "attempt_count",
                    }:
                        setattr(job_row, key, value)
                for key, value in queue_values.items():
                    if key not in {"queue_id", "job_id", "created_at", "attempt_count"}:
                        setattr(queue_row, key, value)
                return True
        except Exception as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError(
                f"Failed to submit historical backfill: {exc}"
            ) from exc

    async def load_backfill_queue_item(
        self,
        queue_id: str,
    ) -> BackfillQueueItem | None:
        async with self._db.session() as session:
            row = await session.get(BackfillQueueItemModel, queue_id)
            return self._queue_item_from_row(row) if row is not None else None

    async def claim_next_backfill(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> BackfillQueueItem | None:
        """Atomically claim one ready item; PostgreSQL workers skip locks."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = now or _now()
        claimable = or_(
            and_(
                BackfillQueueItemModel.status.in_(("PENDING", "RETRY")),
                BackfillQueueItemModel.available_at <= now,
                BackfillQueueItemModel.attempt_count
                < BackfillQueueItemModel.max_attempts,
            ),
            and_(
                BackfillQueueItemModel.status == "LEASED",
                BackfillQueueItemModel.lease_expires_at <= now,
                BackfillQueueItemModel.attempt_count
                < BackfillQueueItemModel.max_attempts,
            ),
        )
        statement = (
            select(BackfillQueueItemModel)
            .where(claimable)
            .order_by(
                BackfillQueueItemModel.available_at,
                BackfillQueueItemModel.created_at,
            )
            .limit(1)
        )
        if self._db.engine.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        try:
            async with self._db.session() as session, session.begin():
                exhausted_statement = select(BackfillQueueItemModel).where(
                    BackfillQueueItemModel.attempt_count
                    >= BackfillQueueItemModel.max_attempts,
                    or_(
                        BackfillQueueItemModel.status.in_(
                            ("PENDING", "RETRY")
                        ),
                        and_(
                            BackfillQueueItemModel.status == "LEASED",
                            BackfillQueueItemModel.lease_expires_at <= now,
                        ),
                    ),
                )
                if self._db.engine.dialect.name == "postgresql":
                    exhausted_statement = exhausted_statement.with_for_update(
                        skip_locked=True
                    )
                else:
                    exhausted_statement = (
                        exhausted_statement.with_for_update()
                    )
                exhausted = await session.scalars(
                    exhausted_statement
                )
                for exhausted_row in exhausted:
                    exhausted_row.status = "DEAD_LETTER"
                    exhausted_row.leased_by = None
                    exhausted_row.lease_expires_at = None
                    exhausted_row.last_error_code = "BACKFILL_ATTEMPTS_EXHAUSTED"
                    exhausted_row.last_error_message = (
                        "Maximum durable backfill attempts were exhausted"
                    )
                    exhausted_row.completed_at = now
                    exhausted_row.updated_at = now
                    exhausted_job = await session.get(
                        HistoricalBackfillJobModel,
                        exhausted_row.job_id,
                        with_for_update=True,
                    )
                    if exhausted_job is not None:
                        exhausted_job.status = "FAILED"
                        exhausted_job.error_code = (
                            "BACKFILL_ATTEMPTS_EXHAUSTED"
                        )
                        exhausted_job.error_message = (
                            "Maximum durable backfill attempts were exhausted"
                        )
                        exhausted_job.completed_at = now
                        exhausted_job.updated_at = now

                row = await session.scalar(statement)
                if row is None:
                    return None
                row.status = "LEASED"
                row.attempt_count += 1
                row.leased_by = worker_id
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.updated_at = now
                row.completed_at = None

                job_row = await session.get(
                    HistoricalBackfillJobModel,
                    row.job_id,
                    with_for_update=True,
                )
                if job_row is None:
                    raise RuntimeError("queue item references a missing job")
                job_row.status = "RUNNING"
                job_row.started_at = now
                job_row.completed_at = None
                job_row.updated_at = now
                await session.flush()
                return self._queue_item_from_row(row)
        except Exception as exc:
            raise DatabaseError(f"Failed to claim historical backfill: {exc}") from exc

    async def finish_backfill_queue_item(
        self,
        *,
        queue_id: str,
        worker_id: str,
        result: HistoricalBackfillJob,
        retryable: bool,
        retry_delay_seconds: float,
        now: datetime | None = None,
    ) -> BackfillQueueItem:
        """Acknowledge, reschedule, or dead-letter a worker-owned lease."""
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        now = now or _now()
        try:
            async with self._db.session() as session, session.begin():
                row = await session.get(
                    BackfillQueueItemModel,
                    queue_id,
                    with_for_update=True,
                )
                if row is None:
                    raise RuntimeError("backfill queue item does not exist")
                if row.status != "LEASED" or row.leased_by != worker_id:
                    raise RuntimeError("backfill lease ownership was lost")
                if result.job_id != row.job_id:
                    raise RuntimeError(
                        "backfill result does not match the leased job"
                    )

                job_row = await session.get(
                    HistoricalBackfillJobModel,
                    row.job_id,
                    with_for_update=True,
                )
                if job_row is None:
                    raise RuntimeError("queue item references a missing job")
                result_values = result.model_dump()
                for key, value in result_values.items():
                    if key not in {
                        "job_id",
                        "request_fingerprint",
                        "created_at",
                    }:
                        setattr(job_row, key, value)

                error_code = result.error_code
                error_message = result.error_message
                if result.status == "PARTIAL" and error_code is None:
                    error_code = "BACKFILL_PARTIAL"
                    error_message = (
                        f"{result.remaining_gap_count} market-data gaps remain"
                    )

                should_retry = (
                    result.status != "COMPLETED"
                    and retryable
                    and row.attempt_count < row.max_attempts
                )
                if result.status == "COMPLETED":
                    row.status = "COMPLETED"
                    row.completed_at = now
                elif should_retry:
                    row.status = "RETRY"
                    row.available_at = now + timedelta(
                        seconds=retry_delay_seconds
                    )
                    row.completed_at = None
                else:
                    row.status = "DEAD_LETTER"
                    row.completed_at = now

                row.leased_by = None
                row.lease_expires_at = None
                row.last_error_code = error_code
                row.last_error_message = (error_message or "")[:500] or None
                row.updated_at = now

                if should_retry:
                    job_row.status = "PENDING"
                    job_row.completed_at = None
                    job_row.updated_at = now
                await session.flush()
                return self._queue_item_from_row(row)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to finish historical backfill queue item: {exc}"
            ) from exc

    async def save_backfill_raw_page(
        self,
        raw_object: RawDataObject,
        link: BackfillRawPageLink,
    ) -> None:
        """Persist object metadata and its immutable lineage edge atomically."""
        object_values = raw_object.model_dump()
        link_values = link.model_dump()
        try:
            async with self._db.session() as session, session.begin():
                object_insert = self._dialect_insert(RawDataObjectModel)
                link_insert = self._dialect_insert(BackfillRawPageModel)
                if object_insert is not None:
                    await session.execute(
                        object_insert.values(**object_values).on_conflict_do_nothing(
                            index_elements=["object_hash"]
                        )
                    )
                elif (
                    await session.get(
                        RawDataObjectModel,
                        raw_object.object_hash,
                    )
                    is None
                ):
                    session.add(RawDataObjectModel(**object_values))
                    await session.flush()

                if link_insert is not None:
                    await session.execute(
                        link_insert.values(**link_values).on_conflict_do_nothing(
                            index_elements=["page_id"]
                        )
                    )
                elif (
                    await session.get(BackfillRawPageModel, link.page_id)
                    is None
                ):
                    session.add(BackfillRawPageModel(**link_values))
        except Exception as exc:
            raise DatabaseError(f"Failed to persist raw page lineage: {exc}") from exc

    async def list_backfill_raw_pages(
        self,
        job_id: str,
    ) -> list[BackfillRawPageLink]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(BackfillRawPageModel)
                .where(BackfillRawPageModel.job_id == job_id)
                .order_by(
                    BackfillRawPageModel.attempt_count,
                    BackfillRawPageModel.page_index,
                )
            )
            return [
                BackfillRawPageLink(
                    schema_version=row.schema_version,
                    page_id=row.page_id,
                    job_id=row.job_id,
                    attempt_count=row.attempt_count,
                    page_index=row.page_index,
                    object_hash=row.object_hash,
                    source=row.source,
                    endpoint=row.endpoint,
                    request_params=row.request_params,
                    fetched_at=_as_utc(row.fetched_at),
                    created_at=_as_utc(row.created_at),
                )
                for row in rows
            ]

    async def load_raw_data_object(
        self,
        object_hash: str,
    ) -> RawDataObject | None:
        async with self._db.session() as session:
            row = await session.get(RawDataObjectModel, object_hash)
            if row is None:
                return None
            return RawDataObject(
                schema_version=row.schema_version,
                object_hash=row.object_hash,
                object_uri=row.object_uri,
                content_type=row.content_type,
                content_encoding=row.content_encoding,
                uncompressed_bytes=row.uncompressed_bytes,
                stored_bytes=row.stored_bytes,
                created_at=_as_utc(row.created_at),
            )

    async def create_agent_execution(
        self,
        job: AgentExecutionJob,
        input_memory: AgentMemoryEntry,
    ) -> AgentExecutionJob:
        """Create one idempotent PAPER job and its first memory entry."""

        if (
            input_memory.execution_id != job.execution_id
            or input_memory.sequence != 1
            or input_memory.entry_type != "INPUT"
        ):
            raise ValueError("Initial agent memory does not match the job")
        values = {
            "execution_id": job.execution_id,
            "request_fingerprint": job.request_fingerprint,
            "schema_version": job.schema_version,
            "runtime_version": job.runtime_version,
            "idempotency_key": job.idempotency_key,
            "correlation_id": job.correlation_id,
            "agent_name": job.agent_name,
            "agent_version": job.agent_version,
            "agent_definition_hash": job.agent_definition_hash,
            "execution_mode": job.execution_mode,
            "decision_role": job.decision_role,
            "critical": job.critical,
            "input_payload": job.input.model_dump(mode="json"),
            "status": job.status,
            "attempt_count": job.attempt_count,
            "max_attempts": job.max_attempts,
            "available_at": job.available_at,
            "leased_by": job.leased_by,
            "lease_expires_at": job.lease_expires_at,
            "last_error_code": job.last_error_code,
            "output_payload": (
                job.output.model_dump(mode="json")
                if job.output is not None
                else None
            ),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(
                    AgentExecutionJobModel
                )
                inserted = False
                if insert_statement is not None:
                    inserted_id = await session.scalar(
                        insert_statement.values(**values)
                        .on_conflict_do_nothing()
                        .returning(AgentExecutionJobModel.execution_id)
                    )
                    inserted = inserted_id is not None
                else:
                    existing = await session.get(
                        AgentExecutionJobModel,
                        job.execution_id,
                    )
                    if existing is None:
                        session.add(AgentExecutionJobModel(**values))
                        await session.flush()
                        inserted = True

                if inserted:
                    session.add(
                        AgentMemoryEntryModel(
                            execution_id=input_memory.execution_id,
                            schema_version=input_memory.schema_version,
                            sequence=input_memory.sequence,
                            entry_type=input_memory.entry_type,
                            payload=input_memory.payload,
                            payload_hash=input_memory.payload_hash,
                            created_at=input_memory.created_at,
                        )
                    )
                    return job

                row = await session.scalar(
                    select(AgentExecutionJobModel).where(
                        AgentExecutionJobModel.agent_name == job.agent_name,
                        AgentExecutionJobModel.agent_version
                        == job.agent_version,
                        AgentExecutionJobModel.idempotency_key
                        == job.idempotency_key,
                    )
                )
                if row is None:
                    row = await session.get(
                        AgentExecutionJobModel,
                        job.execution_id,
                    )
                if row is None:
                    raise RuntimeError(
                        "Agent execution conflict has no stored job"
                    )
                if row.request_fingerprint != job.request_fingerprint:
                    raise ValidationError(
                        "Agent execution idempotency key conflicts with "
                        "different input"
                    )
                return self._agent_execution_job_from_row(row)
        except Exception as exc:
            if isinstance(exc, (DatabaseError, ValidationError)):
                raise
            raise DatabaseError(
                f"Failed to create agent execution: {exc}"
            ) from exc

    async def _claim_agent_execution(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        execution_id: str | None,
    ) -> AgentExecutionJob | None:
        if lease_seconds < 1:
            raise ValueError("Agent lease_seconds must be positive")
        now = _now()
        ready = and_(
            AgentExecutionJobModel.status.in_(("PENDING", "RETRY")),
            AgentExecutionJobModel.available_at <= now,
            AgentExecutionJobModel.attempt_count
            < AgentExecutionJobModel.max_attempts,
        )
        expired = and_(
            AgentExecutionJobModel.status == "LEASED",
            AgentExecutionJobModel.lease_expires_at <= now,
        )
        statement = (
            select(AgentExecutionJobModel)
            .where(or_(ready, expired))
            .order_by(
                AgentExecutionJobModel.available_at,
                AgentExecutionJobModel.created_at,
            )
            .limit(1)
        )
        if execution_id is not None:
            statement = statement.where(
                AgentExecutionJobModel.execution_id == execution_id
            )
        if self._db.engine.dialect.name == "postgresql":
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        try:
            async with self._db.session() as session, session.begin():
                row = await session.scalar(statement)
                if row is None:
                    return None
                reclaimed = row.status == "LEASED"
                row.status = "LEASED"
                if not reclaimed:
                    row.attempt_count += 1
                row.leased_by = worker_id
                row.lease_expires_at = now + timedelta(
                    seconds=lease_seconds
                )
                row.updated_at = now
                row.completed_at = None
                await session.flush()
                return self._agent_execution_job_from_row(row)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to claim agent execution: {exc}"
            ) from exc

    async def claim_agent_execution(
        self,
        execution_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None:
        return await self._claim_agent_execution(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            execution_id=execution_id,
        )

    async def claim_next_agent_execution(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AgentExecutionJob | None:
        return await self._claim_agent_execution(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            execution_id=None,
        )

    async def finish_agent_execution(
        self,
        *,
        attempt: AgentExecutionAttempt,
        attempt_memory: AgentMemoryEntry,
        worker_id: str,
        output: AgentOutput,
        retryable: bool,
        retry_delay_seconds: float,
        terminal_memory: AgentMemoryEntry | None,
    ) -> AgentExecutionJob:
        """Atomically append evidence and acknowledge/retry/dead-letter."""

        if retry_delay_seconds < 0:
            raise ValueError("Agent retry delay must not be negative")
        if (
            attempt.execution_id != attempt_memory.execution_id
            or attempt.output != output
            or attempt.worker_id != worker_id
            or attempt_memory.sequence != attempt.attempt_number * 2
            or attempt_memory.entry_type != "ATTEMPT"
        ):
            raise ValueError("Agent attempt evidence is inconsistent")
        now = _now()
        try:
            async with self._db.session() as session, session.begin():
                row = await session.get(
                    AgentExecutionJobModel,
                    attempt.execution_id,
                    with_for_update=True,
                )
                if row is None:
                    raise RuntimeError("Agent execution does not exist")
                if row.status != "LEASED" or row.leased_by != worker_id:
                    raise RuntimeError("Agent execution lease ownership lost")
                if row.attempt_count != attempt.attempt_number:
                    raise RuntimeError(
                        "Agent attempt does not match leased attempt"
                    )
                prior_attempts = await session.scalar(
                    select(func.count())
                    .select_from(AgentExecutionAttemptModel)
                    .where(
                        AgentExecutionAttemptModel.execution_id
                        == attempt.execution_id
                    )
                )
                if prior_attempts != attempt.attempt_number - 1:
                    raise RuntimeError(
                        "Agent attempts are not append-only"
                    )

                session.add(
                    AgentExecutionAttemptModel(
                        execution_id=attempt.execution_id,
                        schema_version=attempt.schema_version,
                        attempt_number=attempt.attempt_number,
                        worker_id=attempt.worker_id,
                        status=attempt.status.value,
                        output_payload=output.model_dump(mode="json"),
                        retryable=attempt.retryable,
                        started_at=attempt.started_at,
                        completed_at=attempt.completed_at,
                    )
                )
                session.add(
                    AgentMemoryEntryModel(
                        execution_id=attempt_memory.execution_id,
                        schema_version=attempt_memory.schema_version,
                        sequence=attempt_memory.sequence,
                        entry_type=attempt_memory.entry_type,
                        payload=attempt_memory.payload,
                        payload_hash=attempt_memory.payload_hash,
                        created_at=attempt_memory.created_at,
                    )
                )

                successful = output.status not in {
                    AgentStatus.FAILED,
                    AgentStatus.TIMEOUT,
                }
                should_retry = (
                    not successful
                    and retryable
                    and row.attempt_count < row.max_attempts
                )
                if successful:
                    row.status = "COMPLETED"
                    row.completed_at = now
                elif should_retry:
                    row.status = "RETRY"
                    row.available_at = now + timedelta(
                        seconds=retry_delay_seconds
                    )
                    row.completed_at = None
                else:
                    row.status = "DEAD_LETTER"
                    row.completed_at = now

                row.leased_by = None
                row.lease_expires_at = None
                row.last_error_code = (
                    None
                    if successful
                    else "AGENT_TIMEOUT"
                    if output.status == AgentStatus.TIMEOUT
                    else "AGENT_FAILED"
                )
                row.output_payload = output.model_dump(mode="json")
                row.updated_at = now

                if row.status in {"COMPLETED", "DEAD_LETTER"}:
                    expected_type = (
                        "OUTPUT"
                        if row.status == "COMPLETED"
                        else "DEAD_LETTER"
                    )
                    if (
                        terminal_memory is None
                        or terminal_memory.execution_id != row.execution_id
                        or terminal_memory.entry_type != expected_type
                        or terminal_memory.sequence
                        != attempt.attempt_number * 2 + 1
                    ):
                        raise RuntimeError(
                            "Terminal agent memory is inconsistent"
                        )
                    session.add(
                        AgentMemoryEntryModel(
                            execution_id=terminal_memory.execution_id,
                            schema_version=terminal_memory.schema_version,
                            sequence=terminal_memory.sequence,
                            entry_type=terminal_memory.entry_type,
                            payload=terminal_memory.payload,
                            payload_hash=terminal_memory.payload_hash,
                            created_at=terminal_memory.created_at,
                        )
                    )
                    session.add(
                        AgentOutputModel(
                            correlation_id=row.correlation_id,
                            agent_name=row.agent_name,
                            status=output.status.value,
                            signal=output.signal.value,
                            confidence=output.confidence,
                            reason=output.reason,
                            evidence=output.evidence,
                            warnings=output.warnings,
                            latency_ms=output.latency_ms,
                            created_at=output.created_at,
                        )
                    )
                elif terminal_memory is not None:
                    raise RuntimeError(
                        "Retrying execution cannot append terminal memory"
                    )
                await session.flush()
                return self._agent_execution_job_from_row(row)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to finish agent execution: {exc}"
            ) from exc

    async def load_agent_execution_trace(
        self,
        execution_id: str,
    ) -> AgentExecutionTrace | None:
        try:
            async with self._db.session() as session:
                row = await session.get(
                    AgentExecutionJobModel,
                    execution_id,
                )
                if row is None:
                    return None
                attempts = list(
                    await session.scalars(
                        select(AgentExecutionAttemptModel)
                        .where(
                            AgentExecutionAttemptModel.execution_id
                            == execution_id
                        )
                        .order_by(
                            AgentExecutionAttemptModel.attempt_number
                        )
                    )
                )
                memory = list(
                    await session.scalars(
                        select(AgentMemoryEntryModel)
                        .where(
                            AgentMemoryEntryModel.execution_id
                            == execution_id
                        )
                        .order_by(AgentMemoryEntryModel.sequence)
                    )
                )
                return AgentExecutionTrace(
                    job=self._agent_execution_job_from_row(row),
                    attempts=[
                        self._agent_attempt_from_row(item)
                        for item in attempts
                    ],
                    memory=[
                        self._agent_memory_from_row(item)
                        for item in memory
                    ],
                )
        except Exception as exc:
            raise DatabaseError(
                f"Failed to load agent execution trace: {exc}"
            ) from exc

    async def list_agent_execution_jobs(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentExecutionJob]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Agent execution limit must be 1..1000")
        try:
            async with self._db.session() as session:
                rows = list(
                    await session.scalars(
                        select(AgentExecutionJobModel)
                        .order_by(
                            AgentExecutionJobModel.created_at.desc()
                        )
                        .limit(limit)
                    )
                )
                return [
                    self._agent_execution_job_from_row(row)
                    for row in rows
                ]
        except Exception as exc:
            raise DatabaseError(
                f"Failed to list agent executions: {exc}"
            ) from exc

    async def save_agent_output(self, correlation_id: str, output: AgentOutput) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    AgentOutputModel(
                        correlation_id=correlation_id,
                        agent_name=output.agent_name,
                        status=output.status.value,
                        signal=output.signal.value,
                        confidence=output.confidence,
                        reason=output.reason,
                        evidence=output.evidence,
                        warnings=output.warnings,
                        latency_ms=output.latency_ms,
                        created_at=output.created_at,
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist agent output: {exc}") from exc

    async def save_decision(self, decision: Decision) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    DecisionModel(
                        id=decision.decision_id,
                        correlation_id=decision.correlation_id,
                        symbol=decision.symbol,
                        timeframe=decision.timeframe,
                        candidate_action=decision.candidate_action.value,
                        confidence=decision.confidence,
                        reason=decision.reason,
                        agent_summary=decision.agent_summary,
                        risk_status=decision.risk_status.value,
                        created_at=decision.created_at,
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist decision: {exc}") from exc

    async def update_decision_risk_status(self, decision_id: str, risk_status: str) -> None:
        try:
            async with self._db.session() as session, session.begin():
                await session.execute(
                    update(DecisionModel)
                    .where(DecisionModel.id == decision_id)
                    .values(risk_status=risk_status)
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to update decision: {exc}") from exc

    async def save_risk_check(self, check: RiskCheck) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    RiskCheckModel(
                        id=check.risk_check_id,
                        decision_id=check.decision_id,
                        correlation_id=check.correlation_id,
                        risk_status=check.risk_status.value,
                        approved=check.approved,
                        position_size=check.position_size,
                        risk_percent=check.risk_percent,
                        stop_loss=check.stop_loss,
                        take_profit=check.take_profit,
                        risk_reward=check.risk_reward,
                        reason=check.reason,
                        warnings=check.warnings,
                        created_at=check.created_at,
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist risk check: {exc}") from exc

    async def save_central_risk_evaluation(
        self,
        check: RiskCheck,
        approval: OrderApproval | None,
    ) -> None:
        """Persist immutable risk evidence and its capability in one transaction."""

        try:
            async with self._db.session() as session, session.begin():
                existing = await session.get(
                    RiskEvaluationModel,
                    check.evaluation_id,
                )
                if existing is not None:
                    if (
                        existing.request_fingerprint
                        != check.request_fingerprint
                        or existing.idempotency_key != check.idempotency_key
                    ):
                        raise ValidationError(
                            "Immutable risk evaluation identity conflict"
                        )
                    return
                key_owner = await session.scalar(
                    select(RiskEvaluationModel).where(
                        RiskEvaluationModel.idempotency_key
                        == check.idempotency_key
                    )
                )
                if key_owner is not None:
                    raise ValidationError(
                        "Risk idempotency key already belongs to another request"
                    )
                session.add(
                    RiskEvaluationModel(
                        evaluation_id=check.evaluation_id,
                        risk_check_id=check.risk_check_id,
                        idempotency_key=check.idempotency_key,
                        request_fingerprint=check.request_fingerprint,
                        decision_id=check.decision_id,
                        correlation_id=check.correlation_id,
                        risk_status=check.risk_status.value,
                        approved=check.approved,
                        payload=check.model_dump(mode="json"),
                        created_at=check.created_at,
                    )
                )
                session.add(
                    RiskCheckModel(
                        id=check.risk_check_id,
                        decision_id=check.decision_id,
                        correlation_id=check.correlation_id,
                        risk_status=check.risk_status.value,
                        approved=check.approved,
                        position_size=check.position_size,
                        risk_percent=check.risk_percent,
                        stop_loss=check.stop_loss,
                        take_profit=check.take_profit,
                        risk_reward=check.risk_reward,
                        reason=check.reason,
                        warnings=check.warnings,
                        created_at=check.created_at,
                    )
                )
                if approval is not None:
                    # The approval references the evaluation, but these models
                    # intentionally have no mutable ORM relationship. Flush the
                    # immutable parent evidence first so PostgreSQL can enforce
                    # the immediate foreign key without splitting the atomic
                    # transaction.
                    await session.flush()
                    session.add(
                        OrderApprovalModel(
                            approval_id=approval.approval_id,
                            evaluation_id=approval.evaluation_id,
                            risk_check_id=approval.risk_check_id,
                            decision_id=approval.decision_id,
                            correlation_id=approval.correlation_id,
                            request_fingerprint=approval.request_fingerprint,
                            position_snapshot_hash=(
                                approval.position_snapshot_hash
                            ),
                            symbol=approval.symbol,
                            timeframe=approval.timeframe,
                            strategy=approval.strategy,
                            side=approval.side.value,
                            max_notional=approval.max_notional,
                            max_leverage=approval.max_leverage,
                            reference_price=approval.reference_price,
                            max_entry_deviation_bps=(
                                approval.max_entry_deviation_bps
                            ),
                            status=approval.status.value,
                            created_at=approval.created_at,
                            expires_at=approval.expires_at,
                            consumed_at=approval.consumed_at,
                            paper_order_id=approval.paper_order_id,
                            oms_order_id=approval.oms_order_id,
                        )
                    )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist central risk evaluation: {exc}"
            ) from exc

    async def load_central_risk_evaluation_by_key(
        self,
        idempotency_key: str,
    ) -> tuple[RiskCheck, OrderApproval | None] | None:
        try:
            async with self._db.session() as session:
                evaluation = await session.scalar(
                    select(RiskEvaluationModel).where(
                        RiskEvaluationModel.idempotency_key
                        == idempotency_key
                    )
                )
                if evaluation is None:
                    return None
                check = RiskCheck.model_validate(evaluation.payload)
                approval_row = await session.scalar(
                    select(OrderApprovalModel).where(
                        OrderApprovalModel.evaluation_id
                        == evaluation.evaluation_id
                    )
                )
                if approval_row is None:
                    return check, None
                approval = OrderApproval(
                    approval_id=approval_row.approval_id,
                    evaluation_id=approval_row.evaluation_id,
                    risk_check_id=approval_row.risk_check_id,
                    decision_id=approval_row.decision_id,
                    correlation_id=approval_row.correlation_id,
                    request_fingerprint=approval_row.request_fingerprint,
                    position_snapshot_hash=(
                        approval_row.position_snapshot_hash
                    ),
                    symbol=approval_row.symbol,
                    timeframe=approval_row.timeframe,
                    strategy=approval_row.strategy,
                    side=OrderSide(approval_row.side),
                    max_notional=float(approval_row.max_notional),
                    max_leverage=float(approval_row.max_leverage),
                    reference_price=float(approval_row.reference_price),
                    max_entry_deviation_bps=float(
                        approval_row.max_entry_deviation_bps
                    ),
                    status=ApprovalStatus(approval_row.status),
                    created_at=approval_row.created_at,
                    expires_at=approval_row.expires_at,
                    consumed_at=approval_row.consumed_at,
                    paper_order_id=approval_row.paper_order_id,
                    oms_order_id=approval_row.oms_order_id,
                )
                return check, approval
        except Exception as exc:
            raise DatabaseError(
                f"Failed to load central risk evaluation: {exc}"
            ) from exc

    @staticmethod
    def _paper_order_values(order: PaperOrder) -> dict:
        return {
            "id": order.paper_order_id,
            "decision_id": order.decision_id,
            "risk_check_id": order.risk_check_id,
            "approval_id": order.approval_id,
            "request_fingerprint": order.request_fingerprint,
            "correlation_id": order.correlation_id,
            "exchange": order.exchange.value,
            "symbol": order.symbol,
            "timeframe": order.timeframe,
            "strategy": order.strategy,
            "side": order.side.value,
            "entry_price": order.entry_price,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "position_size": order.position_size,
            "leverage": order.leverage,
            "status": order.status.value,
            "fees_estimated": order.fees_estimated,
            "slippage_estimated": order.slippage_estimated,
            "opened_at": order.opened_at,
            "closed_at": order.closed_at,
            "pnl": order.pnl,
            "created_at": order.created_at,
        }

    async def consume_order_approval(
        self,
        approval: OrderApproval,
        order: PaperOrder,
        *,
        oms_order: OMSOrder | None = None,
    ) -> None:
        """Consume approval and insert PAPER plus its OMS mirror atomically."""

        if oms_order is not None and (
            oms_order.environment != ExecutionEnvironment.PAPER
            or oms_order.oms_order_id != order.paper_order_id
            or oms_order.approval_id != approval.approval_id
        ):
            raise ValidationError("Invalid PAPER OMS mirror")
        try:
            async with self._db.session() as session, session.begin():
                control = await session.get(
                    RiskControlStateModel,
                    1,
                    with_for_update=True,
                )
                if control is not None and control.active:
                    raise ValidationError(
                        "Durable kill switch prevents order approval"
                    )
                stored = await session.scalar(
                    select(OrderApprovalModel)
                    .where(
                        OrderApprovalModel.approval_id
                        == approval.approval_id
                    )
                    .with_for_update()
                )
                if stored is None:
                    raise ValidationError("Order approval is not durable")
                if stored.status != ApprovalStatus.ACTIVE.value:
                    raise ValidationError(
                        f"Order approval is {stored.status}"
                    )
                exposures = await self._durable_position_exposures(
                    session,
                    lock_paper=True,
                )
                if stored.position_snapshot_hash != _exposure_snapshot_hash(exposures):
                    raise ValidationError(
                        "Order approval is stale for the durable portfolio"
                    )
                now = _now()
                if _as_utc(stored.expires_at) <= now:
                    stored.status = ApprovalStatus.EXPIRED.value
                    raise ValidationError("Order approval expired")
                if (
                    stored.risk_check_id != order.risk_check_id
                    or stored.decision_id != order.decision_id
                    or stored.request_fingerprint
                    != order.request_fingerprint
                    or stored.symbol != order.symbol
                    or stored.strategy != order.strategy
                    or stored.side != order.side.value
                    or float(stored.max_notional)
                    + 1e-8
                    < order.position_size
                    or float(stored.max_leverage) + 1e-8 < order.leverage
                ):
                    raise ValidationError(
                        "Order payload differs from durable approval"
                    )
                if await session.get(PaperOrderModel, order.paper_order_id):
                    raise ValidationError(
                        "Paper order already exists for this approval"
                    )
                stored.status = ApprovalStatus.CONSUMED.value
                stored.consumed_at = approval.consumed_at
                stored.paper_order_id = order.paper_order_id
                stored.oms_order_id = None
                session.add(PaperOrderModel(**self._paper_order_values(order)))
                if oms_order is not None:
                    session.add(
                        OMSOrderModel(**self._oms_order_values(oms_order))
                    )
                    await session.flush()
                    session.add(
                        self._oms_order_event(oms_order, "PAPER_FILLED")
                    )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to consume order approval: {exc}"
            ) from exc

    async def save_paper_order(self, order: PaperOrder) -> None:
        try:
            async with self._db.session() as session, session.begin():
                existing = await session.get(PaperOrderModel, order.paper_order_id)
                if existing is not None:
                    existing.status = order.status.value
                    existing.closed_at = order.closed_at
                    existing.pnl = order.pnl
                    existing.fees_estimated = order.fees_estimated
                else:
                    session.add(PaperOrderModel(**self._paper_order_values(order)))
        except Exception as exc:
            raise DatabaseError(f"Failed to persist paper order: {exc}") from exc

    async def load_open_position_exposures(self) -> list[PositionExposure]:
        try:
            async with self._db.session() as session:
                return await self._durable_position_exposures(
                    session,
                    lock_paper=False,
                )
        except Exception as exc:
            raise DatabaseError(
                f"Failed to restore open risk exposure: {exc}"
            ) from exc

    @staticmethod
    def _oms_order_values(order: OMSOrder) -> dict:
        return {
            "oms_order_id": order.oms_order_id,
            "client_order_id": order.client_order_id,
            "decision_id": order.decision_id,
            "risk_check_id": order.risk_check_id,
            "approval_id": order.approval_id,
            "request_fingerprint": order.request_fingerprint,
            "correlation_id": order.correlation_id,
            "exchange": order.exchange.value,
            "environment": order.environment.value,
            "symbol": order.symbol,
            "timeframe": order.timeframe,
            "strategy": order.strategy,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "time_in_force": order.time_in_force.value,
            "quantity": order.quantity,
            "requested_notional": order.requested_notional,
            "leverage": order.leverage,
            "limit_price": order.limit_price,
            "reference_price": order.reference_price,
            "status": order.status.value,
            "venue_order_id": order.venue_order_id,
            "cumulative_filled_quantity": order.cumulative_filled_quantity,
            "average_fill_price": order.average_fill_price,
            "rejection_reason": order.rejection_reason,
            "state_version": order.state_version,
            "created_at": order.created_at,
            "updated_at": order.updated_at,
            "submitted_at": order.submitted_at,
            "terminal_at": order.terminal_at,
        }

    @staticmethod
    def _oms_order_from_row(row: OMSOrderModel) -> OMSOrder:
        return OMSOrder(
            oms_order_id=row.oms_order_id,
            client_order_id=row.client_order_id,
            decision_id=row.decision_id,
            risk_check_id=row.risk_check_id,
            approval_id=row.approval_id,
            request_fingerprint=row.request_fingerprint,
            correlation_id=row.correlation_id,
            exchange=Exchange(row.exchange),
            environment=ExecutionEnvironment(row.environment),
            symbol=row.symbol,
            timeframe=row.timeframe,
            strategy=row.strategy,
            side=OrderSide(row.side),
            order_type=OMSOrderType(row.order_type),
            time_in_force=OMSTimeInForce(row.time_in_force),
            quantity=float(row.quantity),
            requested_notional=float(row.requested_notional),
            leverage=float(row.leverage),
            limit_price=(
                float(row.limit_price) if row.limit_price is not None else None
            ),
            reference_price=float(row.reference_price),
            status=OMSOrderStatus(row.status),
            venue_order_id=row.venue_order_id,
            cumulative_filled_quantity=float(row.cumulative_filled_quantity),
            average_fill_price=(
                float(row.average_fill_price)
                if row.average_fill_price is not None
                else None
            ),
            rejection_reason=row.rejection_reason,
            state_version=row.state_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            submitted_at=row.submitted_at,
            terminal_at=row.terminal_at,
        )

    @staticmethod
    def _execution_command_values(command: ExecutionCommand) -> dict:
        return {
            "command_id": command.command_id,
            "oms_order_id": command.oms_order_id,
            "command_type": command.command_type.value,
            "status": command.status.value,
            "attempt_count": command.attempt_count,
            "max_attempts": command.max_attempts,
            "leased_by": command.leased_by,
            "lease_expires_at": command.lease_expires_at,
            "available_at": command.available_at,
            "last_error_type": command.last_error_type,
            "created_at": command.created_at,
            "completed_at": command.completed_at,
        }

    @staticmethod
    def _execution_command_from_row(
        row: ExecutionCommandModel,
    ) -> ExecutionCommand:
        return ExecutionCommand(
            command_id=row.command_id,
            oms_order_id=row.oms_order_id,
            command_type=ExecutionCommandType(row.command_type),
            status=ExecutionCommandStatus(row.status),
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            leased_by=row.leased_by,
            lease_expires_at=row.lease_expires_at,
            available_at=row.available_at,
            last_error_type=row.last_error_type,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

    @staticmethod
    def _oms_order_event(
        order: OMSOrder,
        event_type: str,
    ) -> OMSOrderEventModel:
        return OMSOrderEventModel(
            event_id=str(uuid4()),
            oms_order_id=order.oms_order_id,
            state_version=order.state_version,
            event_type=event_type,
            status=order.status.value,
            payload=order.model_dump(mode="json"),
            created_at=order.updated_at,
        )

    @staticmethod
    def _same_oms_identity(left: OMSOrder, right: OMSOrder) -> bool:
        immutable_fields = (
            "oms_order_id",
            "client_order_id",
            "decision_id",
            "risk_check_id",
            "approval_id",
            "request_fingerprint",
            "correlation_id",
            "exchange",
            "environment",
            "symbol",
            "timeframe",
            "strategy",
            "side",
            "order_type",
            "time_in_force",
            "quantity",
            "requested_notional",
            "leverage",
            "limit_price",
            "reference_price",
        )
        return all(getattr(left, field) == getattr(right, field) for field in immutable_fields)

    async def create_oms_order(
        self,
        order: OMSOrder,
        *,
        command: ExecutionCommand | None = None,
        consume_approval: bool,
    ) -> OMSOrder:
        """Create one durable OMS identity and optionally consume risk authority."""

        if command is not None and command.oms_order_id != order.oms_order_id:
            raise ValidationError("Execution command belongs to another OMS order")
        try:
            async with self._db.session() as session, session.begin():
                existing_row = await session.scalar(
                    select(OMSOrderModel).where(
                        or_(
                            OMSOrderModel.oms_order_id == order.oms_order_id,
                            OMSOrderModel.approval_id == order.approval_id,
                            and_(
                                OMSOrderModel.exchange == order.exchange.value,
                                OMSOrderModel.environment
                                == order.environment.value,
                                OMSOrderModel.client_order_id
                                == order.client_order_id,
                            ),
                        )
                    )
                )
                if existing_row is not None:
                    existing = self._oms_order_from_row(existing_row)
                    if not self._same_oms_identity(existing, order):
                        raise ValidationError(
                            "OMS idempotency identity has conflicting payload"
                        )
                    return existing

                stored = await session.scalar(
                    select(OrderApprovalModel)
                    .where(OrderApprovalModel.approval_id == order.approval_id)
                    .with_for_update()
                )
                if stored is None:
                    raise ValidationError("Order approval is not durable")

                if consume_approval:
                    if order.environment != ExecutionEnvironment.TESTNET:
                        raise ValidationError(
                            "Only TESTNET OMS orders consume approval directly"
                        )
                    control = await session.get(
                        RiskControlStateModel,
                        1,
                        with_for_update=True,
                    )
                    if control is not None and control.active:
                        raise ValidationError(
                            "Durable kill switch prevents OMS submission"
                        )
                    if stored.status != ApprovalStatus.ACTIVE.value:
                        raise ValidationError(
                            f"Order approval is {stored.status}"
                        )
                    exposures = await self._durable_position_exposures(
                        session,
                        lock_paper=True,
                    )
                    if stored.position_snapshot_hash != _exposure_snapshot_hash(
                        exposures
                    ):
                        raise ValidationError(
                            "Order approval is stale for the durable portfolio"
                        )
                    now = _now()
                    if _as_utc(stored.expires_at) <= now:
                        stored.status = ApprovalStatus.EXPIRED.value
                        raise ValidationError("Order approval expired")
                    deviation_bps = (
                        abs(order.reference_price - float(stored.reference_price))
                        / float(stored.reference_price)
                        * 10_000
                    )
                    if (
                        stored.risk_check_id != order.risk_check_id
                        or stored.decision_id != order.decision_id
                        or stored.request_fingerprint
                        != order.request_fingerprint
                        or stored.correlation_id != order.correlation_id
                        or stored.symbol != order.symbol
                        or stored.timeframe != order.timeframe
                        or stored.strategy != order.strategy
                        or stored.side != order.side.value
                        or float(stored.max_notional) + 1e-8
                        < order.requested_notional
                        or float(stored.max_leverage) + 1e-8
                        < order.leverage
                        or deviation_bps
                        > float(stored.max_entry_deviation_bps) + 1e-8
                    ):
                        raise ValidationError(
                            "OMS order differs from durable approval"
                        )
                    stored.status = ApprovalStatus.CONSUMED.value
                    stored.consumed_at = now
                    stored.paper_order_id = None
                    stored.oms_order_id = order.oms_order_id
                else:
                    if order.environment != ExecutionEnvironment.PAPER:
                        raise ValidationError(
                            "Unconsumed OMS mirror must be PAPER"
                        )
                    if (
                        stored.status != ApprovalStatus.CONSUMED.value
                        or stored.paper_order_id != order.oms_order_id
                        or stored.oms_order_id is not None
                    ):
                        raise ValidationError(
                            "PAPER OMS mirror requires its consumed approval"
                        )

                session.add(OMSOrderModel(**self._oms_order_values(order)))
                await session.flush()
                session.add(self._oms_order_event(order, "CREATED"))
                if command is not None:
                    session.add(
                        ExecutionCommandModel(
                            **self._execution_command_values(command)
                        )
                    )
                return order
        except DatabaseError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to create OMS order: {exc}") from exc

    async def load_oms_order(self, oms_order_id: str) -> OMSOrder | None:
        try:
            async with self._db.session() as session:
                row = await session.get(OMSOrderModel, oms_order_id)
                return self._oms_order_from_row(row) if row is not None else None
        except Exception as exc:
            raise DatabaseError(f"Failed to load OMS order: {exc}") from exc

    async def list_oms_orders(
        self,
        *,
        exchange: Exchange | None = None,
        environment: ExecutionEnvironment | None = None,
        limit: int | None = 200,
    ) -> list[OMSOrder]:
        try:
            async with self._db.session() as session:
                statement = select(OMSOrderModel)
                if exchange is not None:
                    statement = statement.where(
                        OMSOrderModel.exchange == exchange.value
                    )
                if environment is not None:
                    statement = statement.where(
                        OMSOrderModel.environment == environment.value
                    )
                statement = statement.order_by(
                    OMSOrderModel.created_at.desc()
                )
                if limit is not None:
                    statement = statement.limit(
                        max(1, min(limit, 1_000))
                    )
                rows = list(await session.scalars(statement))
                return [self._oms_order_from_row(row) for row in rows]
        except Exception as exc:
            raise DatabaseError(f"Failed to list OMS orders: {exc}") from exc

    async def claim_execution_command(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> tuple[ExecutionCommand, OMSOrder] | None:
        """Lease one command; PostgreSQL workers skip rows leased elsewhere."""

        now = _now()
        lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
        try:
            async with self._db.session() as session, session.begin():
                expired_rows = list(
                    await session.scalars(
                        select(ExecutionCommandModel)
                        .where(
                            ExecutionCommandModel.status
                            == ExecutionCommandStatus.LEASED.value,
                            ExecutionCommandModel.lease_expires_at <= now,
                            ExecutionCommandModel.attempt_count
                            >= ExecutionCommandModel.max_attempts,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(100)
                    )
                )
                for expired in expired_rows:
                    expired_order_row = await session.get(
                        OMSOrderModel,
                        expired.oms_order_id,
                        with_for_update=True,
                    )
                    if expired_order_row is None:
                        raise ValidationError(
                            "Expired command references missing OMS order"
                        )
                    expired_order = self._oms_order_from_row(
                        expired_order_row
                    )
                    if expired_order.status not in TERMINAL_OMS_STATUSES:
                        unknown = expired_order.model_copy(
                            update={
                                "status": OMSOrderStatus.UNKNOWN,
                                "state_version": (
                                    expired_order.state_version + 1
                                ),
                                "updated_at": now,
                                "terminal_at": None,
                                "rejection_reason": None,
                            }
                        )
                        expired_order_row.status = unknown.status.value
                        expired_order_row.state_version = (
                            unknown.state_version
                        )
                        expired_order_row.updated_at = unknown.updated_at
                        expired_order_row.terminal_at = None
                        expired_order_row.rejection_reason = None
                        session.add(
                            self._oms_order_event(
                                unknown,
                                "LEASE_EXPIRED_STATUS_UNKNOWN",
                            )
                        )
                    expired.status = (
                        ExecutionCommandStatus.DEAD_LETTER.value
                    )
                    expired.leased_by = None
                    expired.lease_expires_at = None
                    expired.last_error_type = "LEASE_EXPIRED"
                    expired.completed_at = now
                statement = (
                    select(ExecutionCommandModel)
                    .where(
                        ExecutionCommandModel.attempt_count
                        < ExecutionCommandModel.max_attempts,
                        ExecutionCommandModel.available_at <= now,
                        or_(
                            ExecutionCommandModel.status
                            == ExecutionCommandStatus.PENDING.value,
                            and_(
                                ExecutionCommandModel.status
                                == ExecutionCommandStatus.LEASED.value,
                                ExecutionCommandModel.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .order_by(
                        ExecutionCommandModel.available_at,
                        ExecutionCommandModel.created_at,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                row = await session.scalar(statement)
                if row is None:
                    return None
                order_row = await session.get(
                    OMSOrderModel,
                    row.oms_order_id,
                    with_for_update=True,
                )
                if order_row is None:
                    raise ValidationError(
                        "Execution command references missing OMS order"
                    )
                row.status = ExecutionCommandStatus.LEASED.value
                row.attempt_count += 1
                row.leased_by = worker_id
                row.lease_expires_at = lease_until
                return (
                    self._execution_command_from_row(row),
                    self._oms_order_from_row(order_row),
                )
        except DatabaseError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to claim execution command: {exc}"
            ) from exc

    async def finish_execution_command(
        self,
        *,
        command_id: str,
        worker_id: str,
        order: OMSOrder,
        event_type: str,
        error_type: str | None = None,
    ) -> OMSOrder:
        try:
            async with self._db.session() as session, session.begin():
                command_row = await session.get(
                    ExecutionCommandModel,
                    command_id,
                    with_for_update=True,
                )
                if (
                    command_row is None
                    or command_row.status
                    != ExecutionCommandStatus.LEASED.value
                    or command_row.leased_by != worker_id
                ):
                    raise ValidationError(
                        "Execution command lease is no longer owned"
                    )
                order_row = await session.get(
                    OMSOrderModel,
                    command_row.oms_order_id,
                    with_for_update=True,
                )
                if order_row is None:
                    raise ValidationError("OMS order no longer exists")
                stored = self._oms_order_from_row(order_row)
                if not self._same_oms_identity(stored, order):
                    raise ValidationError("OMS transition changed immutable identity")
                if order.state_version != stored.state_version + 1:
                    raise ValidationError(
                        "OMS transition must advance exactly one version"
                    )
                mutable_fields = (
                    "status",
                    "venue_order_id",
                    "cumulative_filled_quantity",
                    "average_fill_price",
                    "rejection_reason",
                    "state_version",
                    "updated_at",
                    "submitted_at",
                    "terminal_at",
                )
                values = self._oms_order_values(order)
                for field in mutable_fields:
                    setattr(order_row, field, values[field])
                command_row.status = ExecutionCommandStatus.COMPLETED.value
                command_row.leased_by = None
                command_row.lease_expires_at = None
                command_row.last_error_type = error_type
                command_row.completed_at = order.updated_at
                session.add(self._oms_order_event(order, event_type))
                return order
        except DatabaseError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to finish execution command: {exc}"
            ) from exc

    async def queue_cancel_command(
        self,
        order: OMSOrder,
        command: ExecutionCommand,
    ) -> OMSOrder:
        if (
            command.command_type != ExecutionCommandType.CANCEL
            or command.oms_order_id != order.oms_order_id
        ):
            raise ValidationError("Invalid OMS cancellation command")
        try:
            async with self._db.session() as session, session.begin():
                row = await session.get(
                    OMSOrderModel,
                    order.oms_order_id,
                    with_for_update=True,
                )
                if row is None:
                    raise ValidationError("OMS order does not exist")
                stored = self._oms_order_from_row(row)
                if not self._same_oms_identity(stored, order):
                    raise ValidationError("OMS cancellation changed identity")
                existing = await session.scalar(
                    select(ExecutionCommandModel).where(
                        ExecutionCommandModel.oms_order_id
                        == order.oms_order_id,
                        ExecutionCommandModel.command_type
                        == ExecutionCommandType.CANCEL.value,
                    )
                )
                if existing is not None:
                    return stored
                if order.state_version != stored.state_version + 1:
                    raise ValidationError(
                        "OMS cancellation must advance exactly one version"
                    )
                values = self._oms_order_values(order)
                row.status = values["status"]
                row.state_version = values["state_version"]
                row.updated_at = values["updated_at"]
                session.add(self._oms_order_event(order, "CANCEL_QUEUED"))
                session.add(
                    ExecutionCommandModel(
                        **self._execution_command_values(command)
                    )
                )
                return order
        except DatabaseError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to queue OMS cancellation: {exc}"
            ) from exc

    async def persist_reconciliation(
        self,
        run: ReconciliationRun,
        *,
        mismatches: list[ReconciliationMismatch],
        snapshot: VenueStateSnapshot,
        reconciled_orders: list[OMSOrder],
    ) -> None:
        """Persist one immutable venue snapshot and its OMS corrections."""

        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    ReconciliationRunModel(
                        run_id=run.run_id,
                        exchange=run.exchange.value,
                        environment=run.environment.value,
                        status=run.status.value,
                        local_order_count=run.local_order_count,
                        venue_order_count=run.venue_order_count,
                        fill_count=run.fill_count,
                        position_count=run.position_count,
                        balance_count=run.balance_count,
                        mismatch_count=run.mismatch_count,
                        critical_mismatch_count=run.critical_mismatch_count,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        error_type=run.error_type,
                    )
                )
                await session.flush()
                for reconciled in reconciled_orders:
                    row = await session.get(
                        OMSOrderModel,
                        reconciled.oms_order_id,
                        with_for_update=True,
                    )
                    if row is None:
                        continue
                    stored = self._oms_order_from_row(row)
                    if (
                        not self._same_oms_identity(stored, reconciled)
                        or reconciled.state_version != stored.state_version + 1
                    ):
                        raise ValidationError(
                            "Invalid OMS reconciliation transition"
                        )
                    values = self._oms_order_values(reconciled)
                    for field in (
                        "status",
                        "venue_order_id",
                        "cumulative_filled_quantity",
                        "average_fill_price",
                        "rejection_reason",
                        "state_version",
                        "updated_at",
                        "submitted_at",
                        "terminal_at",
                    ):
                        setattr(row, field, values[field])
                    session.add(
                        self._oms_order_event(reconciled, "RECONCILED")
                    )

                scoped_orders = (
                    reconciled_orders
                    + await self._load_oms_orders_in_session(
                        session,
                        exchange=run.exchange,
                        environment=run.environment,
                    )
                )
                order_ids_by_client = {
                    order.client_order_id: order.oms_order_id
                    for order in scoped_orders
                }
                order_ids_by_venue = {
                    order.venue_order_id: order.oms_order_id
                    for order in scoped_orders
                    if order.venue_order_id is not None
                }
                scoped_order_ids = {
                    order.oms_order_id for order in scoped_orders
                }
                for fill in snapshot.fills:
                    associated_order_id = (
                        order_ids_by_client.get(
                            fill.client_order_id or ""
                        )
                        or order_ids_by_venue.get(fill.venue_order_id)
                        or (
                            fill.oms_order_id
                            if fill.oms_order_id in scoped_order_ids
                            else None
                        )
                    )
                    fill_values = {
                        "fill_id": fill.fill_id,
                        "oms_order_id": associated_order_id,
                        "venue_order_id": fill.venue_order_id,
                        "client_order_id": fill.client_order_id,
                        "exchange": fill.exchange.value,
                        "environment": fill.environment.value,
                        "symbol": fill.symbol,
                        "side": fill.side.value,
                        "quantity": fill.quantity,
                        "price": fill.price,
                        "fee": fill.fee,
                        "fee_asset": fill.fee_asset,
                        "occurred_at": fill.occurred_at,
                        "observed_at": fill.observed_at,
                    }
                    statement = self._dialect_insert(ExecutionFillModel)
                    if statement is None:
                        if await session.get(ExecutionFillModel, fill.fill_id):
                            continue
                        session.add(ExecutionFillModel(**fill_values))
                    else:
                        await session.execute(
                            statement.values(**fill_values).on_conflict_do_nothing(
                                index_elements=["fill_id"]
                            )
                        )
                for mismatch in mismatches:
                    session.add(
                        ReconciliationMismatchModel(
                            mismatch_id=mismatch.mismatch_id,
                            run_id=run.run_id,
                            mismatch_type=mismatch.mismatch_type.value,
                            severity=mismatch.severity.value,
                            exchange=mismatch.exchange.value,
                            environment=mismatch.environment.value,
                            oms_order_id=mismatch.oms_order_id,
                            venue_order_id=mismatch.venue_order_id,
                            symbol=mismatch.symbol,
                            expected=mismatch.expected,
                            observed=mismatch.observed,
                            created_at=mismatch.created_at,
                        )
                    )
                for position in snapshot.positions:
                    session.add(
                        VenuePositionSnapshotModel(
                            snapshot_id=str(uuid4()),
                            run_id=run.run_id,
                            exchange=position.exchange.value,
                            environment=position.environment.value,
                            symbol=position.symbol,
                            side=position.side.value,
                            quantity=position.quantity,
                            entry_price=position.entry_price,
                            mark_price=position.mark_price,
                            unrealized_pnl=position.unrealized_pnl,
                            observed_at=position.observed_at,
                        )
                    )
                for balance in snapshot.balances:
                    session.add(
                        VenueBalanceSnapshotModel(
                            snapshot_id=str(uuid4()),
                            run_id=run.run_id,
                            exchange=balance.exchange.value,
                            environment=balance.environment.value,
                            asset=balance.asset,
                            available=balance.available,
                            locked=balance.locked,
                            equity=balance.equity,
                            observed_at=balance.observed_at,
                        )
                    )
        except DatabaseError:
            raise
        except ValidationError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist reconciliation: {exc}"
            ) from exc

    async def _load_oms_orders_in_session(
        self,
        session,
        *,
        exchange: Exchange,
        environment: ExecutionEnvironment,
    ) -> list[OMSOrder]:
        rows = list(
            await session.scalars(
                select(OMSOrderModel).where(
                    OMSOrderModel.exchange == exchange.value,
                    OMSOrderModel.environment == environment.value,
                )
            )
        )
        return [self._oms_order_from_row(row) for row in rows]

    async def load_execution_fills(
        self,
        *,
        oms_order_id: str | None = None,
        limit: int = 500,
    ) -> list[ExecutionFill]:
        try:
            async with self._db.session() as session:
                statement = select(ExecutionFillModel)
                if oms_order_id is not None:
                    statement = statement.where(
                        ExecutionFillModel.oms_order_id == oms_order_id
                    )
                rows = list(
                    await session.scalars(
                        statement.order_by(
                            ExecutionFillModel.occurred_at.desc()
                        ).limit(max(1, min(limit, 2_000)))
                    )
                )
                return [
                    ExecutionFill(
                        fill_id=row.fill_id,
                        oms_order_id=row.oms_order_id,
                        venue_order_id=row.venue_order_id,
                        client_order_id=row.client_order_id,
                        exchange=Exchange(row.exchange),
                        environment=ExecutionEnvironment(row.environment),
                        symbol=row.symbol,
                        side=OrderSide(row.side),
                        quantity=float(row.quantity),
                        price=float(row.price),
                        fee=float(row.fee),
                        fee_asset=row.fee_asset,
                        occurred_at=row.occurred_at,
                        observed_at=row.observed_at,
                    )
                    for row in rows
                ]
        except Exception as exc:
            raise DatabaseError(f"Failed to load execution fills: {exc}") from exc

    async def load_latest_reconciliation(
        self,
        *,
        exchange: Exchange,
        environment: ExecutionEnvironment,
    ) -> tuple[
        ReconciliationRun,
        list[ReconciliationMismatch],
        list[VenuePositionSnapshot],
        list[VenueBalanceSnapshot],
    ] | None:
        try:
            async with self._db.session() as session:
                run_row = await session.scalar(
                    select(ReconciliationRunModel)
                    .where(
                        ReconciliationRunModel.exchange == exchange.value,
                        ReconciliationRunModel.environment == environment.value,
                    )
                    .order_by(ReconciliationRunModel.started_at.desc())
                    .limit(1)
                )
                if run_row is None:
                    return None
                mismatch_rows = list(
                    await session.scalars(
                        select(ReconciliationMismatchModel).where(
                            ReconciliationMismatchModel.run_id
                            == run_row.run_id
                        )
                    )
                )
                position_rows = list(
                    await session.scalars(
                        select(VenuePositionSnapshotModel).where(
                            VenuePositionSnapshotModel.run_id == run_row.run_id
                        )
                    )
                )
                balance_rows = list(
                    await session.scalars(
                        select(VenueBalanceSnapshotModel).where(
                            VenueBalanceSnapshotModel.run_id == run_row.run_id
                        )
                    )
                )
                run = ReconciliationRun(
                    run_id=run_row.run_id,
                    exchange=Exchange(run_row.exchange),
                    environment=ExecutionEnvironment(run_row.environment),
                    status=ReconciliationRunStatus(run_row.status),
                    local_order_count=run_row.local_order_count,
                    venue_order_count=run_row.venue_order_count,
                    fill_count=run_row.fill_count,
                    position_count=run_row.position_count,
                    balance_count=run_row.balance_count,
                    mismatch_count=run_row.mismatch_count,
                    critical_mismatch_count=run_row.critical_mismatch_count,
                    started_at=run_row.started_at,
                    completed_at=run_row.completed_at,
                    error_type=run_row.error_type,
                )
                mismatches = [
                    ReconciliationMismatch(
                        mismatch_id=row.mismatch_id,
                        run_id=row.run_id,
                        mismatch_type=ReconciliationMismatchType(
                            row.mismatch_type
                        ),
                        severity=ReconciliationSeverity(row.severity),
                        exchange=Exchange(row.exchange),
                        environment=ExecutionEnvironment(row.environment),
                        oms_order_id=row.oms_order_id,
                        venue_order_id=row.venue_order_id,
                        symbol=row.symbol,
                        expected=row.expected,
                        observed=row.observed,
                        created_at=row.created_at,
                    )
                    for row in mismatch_rows
                ]
                positions = [
                    VenuePositionSnapshot(
                        exchange=Exchange(row.exchange),
                        environment=ExecutionEnvironment(row.environment),
                        symbol=row.symbol,
                        side=OrderSide(row.side),
                        quantity=float(row.quantity),
                        entry_price=(
                            float(row.entry_price)
                            if row.entry_price is not None
                            else None
                        ),
                        mark_price=(
                            float(row.mark_price)
                            if row.mark_price is not None
                            else None
                        ),
                        unrealized_pnl=float(row.unrealized_pnl),
                        observed_at=row.observed_at,
                    )
                    for row in position_rows
                ]
                balances = [
                    VenueBalanceSnapshot(
                        exchange=Exchange(row.exchange),
                        environment=ExecutionEnvironment(row.environment),
                        asset=row.asset,
                        available=float(row.available),
                        locked=float(row.locked),
                        equity=float(row.equity),
                        observed_at=row.observed_at,
                    )
                    for row in balance_rows
                ]
                return run, mismatches, positions, balances
        except Exception as exc:
            raise DatabaseError(
                f"Failed to load latest reconciliation: {exc}"
            ) from exc

    async def load_risk_control_state(self) -> RiskControlState | None:
        try:
            async with self._db.session() as session:
                row = await session.get(RiskControlStateModel, 1)
                if row is None:
                    return None
                return RiskControlState(
                    active=row.active,
                    revision=row.revision,
                    reason=row.reason,
                    actor=row.actor,
                    triggered_at=row.triggered_at,
                    reset_at=row.reset_at,
                )
        except Exception as exc:
            raise DatabaseError(
                f"Failed to restore risk control: {exc}"
            ) from exc

    async def set_risk_control(
        self,
        *,
        active: bool,
        reason: str,
        actor: str,
        correlation_id: str | None,
    ) -> RiskControlState:
        """Serialize kill-switch transitions and revoke capabilities on trigger."""

        try:
            now = _now()
            async with self._db.session() as session, session.begin():
                row = await session.get(
                    RiskControlStateModel,
                    1,
                    with_for_update=True,
                )
                if row is None:
                    row = RiskControlStateModel(
                        singleton_id=1,
                        active=False,
                        revision=0,
                        updated_at=now,
                    )
                    session.add(row)
                    await session.flush()
                if row.active == active:
                    raise ValidationError(
                        "Risk control is already in the requested state"
                    )
                row.active = active
                row.revision += 1
                row.reason = reason
                row.actor = actor
                row.updated_at = now
                if active:
                    row.triggered_at = now
                    row.reset_at = None
                    await session.execute(
                        update(OrderApprovalModel)
                        .where(
                            OrderApprovalModel.status
                            == ApprovalStatus.ACTIVE.value
                        )
                        .values(status=ApprovalStatus.REVOKED.value)
                    )
                else:
                    row.reset_at = now
                session.add(
                    RiskControlEventModel(
                        event_id=str(uuid4()),
                        revision=row.revision,
                        event_type="TRIGGERED" if active else "RESET",
                        reason=reason,
                        actor=actor,
                        correlation_id=correlation_id,
                        created_at=now,
                    )
                )
                await session.flush()
                return RiskControlState(
                    active=row.active,
                    revision=row.revision,
                    reason=row.reason,
                    actor=row.actor,
                    triggered_at=row.triggered_at,
                    reset_at=row.reset_at,
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to update risk control: {exc}"
            ) from exc

    async def save_audit_log(self, record: dict) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    AuditLogModel(
                        id=record["audit_id"],
                        correlation_id=record["correlation_id"],
                        audit_type=record["audit_type"],
                        entity_type=record["entity_type"],
                        entity_id=record.get("entity_id"),
                        payload=record.get("payload", {}),
                        created_at=_now(),
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist audit log: {exc}") from exc

    async def save_walk_forward_report(
        self,
        report: WalkForwardReport,
    ) -> WalkForwardReport:
        """Insert an immutable artifact or return the identical stored copy."""

        expected_hash = walk_forward_artifact_hash(report)
        if report.artifact_hash != expected_hash:
            raise DatabaseError(
                "Walk-forward artifact_hash does not match report content"
            )
        metadata = WalkForwardArtifactMetadata(
            experiment_id=report.experiment_id,
            artifact_hash=report.artifact_hash,
            protocol_version=report.protocol.protocol_version,
            dataset_id=report.dataset_id,
            dataset_hash=report.dataset_hash,
            symbol=report.symbol,
            timeframe=report.timeframe,
            candidate_version=report.candidate_version,
            promotion_status=report.promotion_status,
            created_at=report.created_at,
            recorded_at=_now(),
        )
        values = {
            **metadata.model_dump(),
            "report_payload": report.model_dump(mode="json"),
        }
        try:
            async with self._db.session() as session, session.begin():
                insert_statement = self._dialect_insert(
                    WalkForwardExperimentModel
                )
                if insert_statement is not None:
                    inserted_id = await session.scalar(
                        insert_statement.values(**values)
                        .on_conflict_do_nothing(
                            index_elements=["experiment_id"]
                        )
                        .returning(
                            WalkForwardExperimentModel.experiment_id
                        )
                    )
                    if inserted_id is not None:
                        return report
                else:
                    existing_id = await session.scalar(
                        select(
                            WalkForwardExperimentModel.experiment_id
                        ).where(
                            WalkForwardExperimentModel.experiment_id
                            == report.experiment_id
                        )
                    )
                    if existing_id is None:
                        session.add(WalkForwardExperimentModel(**values))
                        await session.flush()
                        return report

                row = await session.scalar(
                    select(WalkForwardExperimentModel).where(
                        WalkForwardExperimentModel.experiment_id
                        == report.experiment_id
                    )
                )
                if row is None:
                    raise RuntimeError(
                        "Walk-forward insert conflict has no stored artifact"
                    )
                stored = self._walk_forward_report_from_row(row)
                if stored.artifact_hash != report.artifact_hash:
                    raise RuntimeError(
                        "Immutable walk-forward experiment identity conflict"
                    )
                return stored
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist walk-forward artifact: {exc}"
            ) from exc

    async def load_walk_forward_report(
        self,
        experiment_id: str,
    ) -> WalkForwardReport | None:
        try:
            async with self._db.session() as session:
                row = await session.scalar(
                    select(WalkForwardExperimentModel).where(
                        WalkForwardExperimentModel.experiment_id
                        == experiment_id
                    )
                )
                if row is None:
                    return None
                return self._walk_forward_report_from_row(row)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to load walk-forward artifact: {exc}"
            ) from exc

    async def list_walk_forward_reports(
        self,
        *,
        limit: int = 100,
    ) -> list[WalkForwardReport]:
        if not 1 <= limit <= 1_000:
            raise ValueError("Walk-forward report limit must be 1..1000")
        try:
            async with self._db.session() as session:
                rows = list(
                    await session.scalars(
                        select(WalkForwardExperimentModel)
                        .order_by(
                            WalkForwardExperimentModel.created_at.desc(),
                            WalkForwardExperimentModel.row_id.desc(),
                        )
                        .limit(limit)
                    )
                )
                return [
                    self._walk_forward_report_from_row(row)
                    for row in rows
                ]
        except Exception as exc:
            raise DatabaseError(
                f"Failed to list walk-forward artifacts: {exc}"
            ) from exc

    @staticmethod
    def _specialist_evidence_from_row(
        row: SpecialistEvidenceModel,
    ) -> SpecialistEvidence:
        return SpecialistEvidence(
            schema_version=row.schema_version,
            evidence_id=row.evidence_id,
            domain=row.domain,
            metric_name=row.metric_name,
            scope=row.scope,
            source=row.source,
            source_event_id=row.source_event_id,
            value=float(row.value),
            unit=row.unit,
            quality_score=row.quality_score,
            observed_at=_as_utc(row.observed_at),
            received_at=_as_utc(row.received_at),
            provenance_uri=row.provenance_uri,
            payload_sha256=row.payload_sha256,
        )

    @staticmethod
    def _agent_forecast_from_row(
        row: AgentForecastModel,
    ) -> AgentForecast:
        return AgentForecast(
            schema_version=row.schema_version,
            forecast_id=row.forecast_id,
            correlation_id=row.correlation_id,
            agent_name=row.agent_name,
            agent_version=row.agent_version,
            definition_hash=row.definition_hash,
            symbol=row.symbol,
            timeframe=row.timeframe,
            signal=row.signal,
            confidence=row.confidence,
            probability_up=float(row.probability_up),
            reference_price=float(row.reference_price),
            forecast_at=_as_utc(row.forecast_at),
            target_at=_as_utc(row.target_at),
            horizon_seconds=row.horizon_seconds,
            decision_role=row.decision_role,
            created_at=_as_utc(row.created_at),
        )

    @staticmethod
    def _agent_forecast_outcome_from_row(
        row: AgentForecastOutcomeModel,
    ) -> AgentForecastOutcome:
        return AgentForecastOutcome(
            schema_version=row.schema_version,
            outcome_id=row.outcome_id,
            forecast_id=row.forecast_id,
            realized_at=_as_utc(row.realized_at),
            realized_price=float(row.realized_price),
            realized_return=float(row.realized_return),
            realized_up=float(row.realized_up),
            correct=row.correct,
            brier_loss=float(row.brier_loss),
            ensemble_probability_up=float(row.ensemble_probability_up),
            ensemble_brier_loss=float(row.ensemble_brier_loss),
            leave_one_out_probability_up=float(
                row.leave_one_out_probability_up
            ),
            leave_one_out_brier_loss=float(row.leave_one_out_brier_loss),
            marginal_contribution=float(row.marginal_contribution),
            cohort_size=row.cohort_size,
            created_at=_as_utc(row.created_at),
        )

    async def save_specialist_evidence(
        self,
        evidence: SpecialistEvidence,
    ) -> SpecialistEvidence:
        """Idempotently append one externally sourced evidence record."""

        try:
            async with self._db.session() as session:
                existing = await session.get(
                    SpecialistEvidenceModel,
                    evidence.evidence_id,
                )
                if existing is not None:
                    stored = self._specialist_evidence_from_row(existing)
                    if stored != evidence:
                        raise ValidationError(
                            "Immutable specialist evidence identity conflict"
                        )
                    return stored
                source_event = await session.scalar(
                    select(SpecialistEvidenceModel).where(
                        SpecialistEvidenceModel.source == evidence.source,
                        SpecialistEvidenceModel.source_event_id
                        == evidence.source_event_id,
                    )
                )
                if source_event is not None:
                    stored = self._specialist_evidence_from_row(source_event)
                    if stored != evidence:
                        raise ValidationError(
                            "Source event already maps to different evidence"
                        )
                    return stored
                session.add(
                    SpecialistEvidenceModel(
                        evidence_id=evidence.evidence_id,
                        schema_version=evidence.schema_version,
                        domain=evidence.domain,
                        metric_name=evidence.metric_name,
                        scope=evidence.scope,
                        source=evidence.source,
                        source_event_id=evidence.source_event_id,
                        value=evidence.value,
                        unit=evidence.unit,
                        quality_score=evidence.quality_score,
                        observed_at=evidence.observed_at,
                        received_at=evidence.received_at,
                        provenance_uri=evidence.provenance_uri,
                        payload_sha256=evidence.payload_sha256,
                    )
                )
                await session.commit()
                return evidence
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist specialist evidence: {exc}"
            ) from exc

    async def list_specialist_evidence(
        self,
        *,
        limit: int = 100,
    ) -> list[SpecialistEvidence]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Specialist evidence limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(SpecialistEvidenceModel)
                    .order_by(
                        SpecialistEvidenceModel.observed_at.desc(),
                        SpecialistEvidenceModel.evidence_id,
                    )
                    .limit(limit)
                )
            )
            return [
                self._specialist_evidence_from_row(row)
                for row in rows
            ]

    async def save_agent_forecasts(
        self,
        forecasts: list[AgentForecast],
    ) -> list[AgentForecast]:
        """Append an idempotent forecast cohort in one transaction."""

        if not forecasts:
            return []
        try:
            async with self._db.session() as session:
                stored: list[AgentForecast] = []
                for forecast in forecasts:
                    existing = await session.get(
                        AgentForecastModel,
                        forecast.forecast_id,
                    )
                    if existing is not None:
                        current = self._agent_forecast_from_row(existing)
                        if current != forecast:
                            raise ValidationError(
                                "Immutable agent forecast identity conflict"
                            )
                        stored.append(current)
                        continue
                    session.add(
                        AgentForecastModel(
                            forecast_id=forecast.forecast_id,
                            schema_version=forecast.schema_version,
                            correlation_id=forecast.correlation_id,
                            agent_name=forecast.agent_name,
                            agent_version=forecast.agent_version,
                            definition_hash=forecast.definition_hash,
                            symbol=forecast.symbol,
                            timeframe=forecast.timeframe,
                            signal=forecast.signal.value,
                            confidence=forecast.confidence,
                            probability_up=forecast.probability_up,
                            reference_price=forecast.reference_price,
                            forecast_at=forecast.forecast_at,
                            target_at=forecast.target_at,
                            horizon_seconds=forecast.horizon_seconds,
                            decision_role=forecast.decision_role,
                            created_at=forecast.created_at,
                        )
                    )
                    stored.append(forecast)
                await session.commit()
                return stored
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist agent forecasts: {exc}"
            ) from exc

    async def list_agent_forecasts(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentForecast]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Agent forecast limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(AgentForecastModel)
                    .order_by(
                        AgentForecastModel.forecast_at.desc(),
                        AgentForecastModel.forecast_id,
                    )
                    .limit(limit)
                )
            )
            return [self._agent_forecast_from_row(row) for row in rows]

    async def save_agent_forecast_outcomes(
        self,
        outcomes: list[AgentForecastOutcome],
    ) -> list[AgentForecastOutcome]:
        """Append realized outcomes; an immutable forecast settles once."""

        if not outcomes:
            return []
        try:
            async with self._db.session() as session:
                stored: list[AgentForecastOutcome] = []
                for outcome in outcomes:
                    existing = await session.get(
                        AgentForecastOutcomeModel,
                        outcome.outcome_id,
                    )
                    if existing is None:
                        existing = await session.scalar(
                            select(AgentForecastOutcomeModel).where(
                                AgentForecastOutcomeModel.forecast_id
                                == outcome.forecast_id
                            )
                        )
                    if existing is not None:
                        current = self._agent_forecast_outcome_from_row(existing)
                        if current != outcome:
                            raise ValidationError(
                                "Forecast already has a different outcome"
                            )
                        stored.append(current)
                        continue
                    session.add(
                        AgentForecastOutcomeModel(
                            outcome_id=outcome.outcome_id,
                            schema_version=outcome.schema_version,
                            forecast_id=outcome.forecast_id,
                            realized_at=outcome.realized_at,
                            realized_price=outcome.realized_price,
                            realized_return=outcome.realized_return,
                            realized_up=outcome.realized_up,
                            correct=outcome.correct,
                            brier_loss=outcome.brier_loss,
                            ensemble_probability_up=(
                                outcome.ensemble_probability_up
                            ),
                            ensemble_brier_loss=outcome.ensemble_brier_loss,
                            leave_one_out_probability_up=(
                                outcome.leave_one_out_probability_up
                            ),
                            leave_one_out_brier_loss=(
                                outcome.leave_one_out_brier_loss
                            ),
                            marginal_contribution=(
                                outcome.marginal_contribution
                            ),
                            cohort_size=outcome.cohort_size,
                            created_at=outcome.created_at,
                        )
                    )
                    stored.append(outcome)
                await session.commit()
                return stored
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist agent forecast outcomes: {exc}"
            ) from exc

    async def list_agent_forecast_outcomes(
        self,
        *,
        limit: int = 100,
    ) -> list[AgentForecastOutcome]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Agent outcome limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(AgentForecastOutcomeModel)
                    .order_by(
                        AgentForecastOutcomeModel.realized_at.desc(),
                        AgentForecastOutcomeModel.outcome_id,
                    )
                    .limit(limit)
                )
            )
            return [
                self._agent_forecast_outcome_from_row(row)
                for row in rows
            ]

    async def _save_governance_artifact(
        self,
        *,
        model_class,
        identity: str,
        payload: dict,
        values: dict,
        contract_class,
        label: str,
    ) -> Any:
        """Idempotently append one strict Month 9 governance artifact."""

        try:
            async with self._db.session() as session:
                existing = await session.get(model_class, identity)
                if existing is not None:
                    stored = contract_class.model_validate(existing.payload)
                    current = contract_class.model_validate(payload)
                    if stored != current:
                        raise ValidationError(
                            f"Immutable {label} identity conflict"
                        )
                    return stored
                session.add(model_class(**values))
                await session.commit()
                return contract_class.model_validate(payload)
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(f"Failed to persist {label}: {exc}") from exc

    async def save_consensus_experiment(
        self,
        experiment: ConsensusExperiment,
    ) -> ConsensusExperiment:
        payload = experiment.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=ConsensusExperimentModel,
            identity=experiment.experiment_id,
            payload=payload,
            values={
                "experiment_id": experiment.experiment_id,
                "schema_version": experiment.schema_version,
                "name": experiment.name,
                "version": experiment.version,
                "mode": experiment.mode,
                "payload": payload,
                "created_at": experiment.created_at,
            },
            contract_class=ConsensusExperiment,
            label="consensus experiment",
        )

    async def save_consensus_experiment_definition(
        self,
        experiment: ConsensusExperiment,
        created_event: ConsensusExperimentEvent,
    ) -> tuple[ConsensusExperiment, ConsensusExperimentEvent]:
        """Atomically append an experiment and its CREATED lifecycle event."""

        if (
            created_event.experiment_id != experiment.experiment_id
            or created_event.event_type != "CREATED"
        ):
            raise ValidationError(
                "Experiment definition requires its matching CREATED event"
            )
        experiment_payload = experiment.model_dump(mode="json")
        event_payload = created_event.model_dump(mode="json")
        try:
            async with self._db.session() as session, session.begin():
                experiment_row = await session.get(
                    ConsensusExperimentModel,
                    experiment.experiment_id,
                )
                if experiment_row is None:
                    session.add(
                        ConsensusExperimentModel(
                            experiment_id=experiment.experiment_id,
                            schema_version=experiment.schema_version,
                            name=experiment.name,
                            version=experiment.version,
                            mode=experiment.mode,
                            payload=experiment_payload,
                            created_at=experiment.created_at,
                        )
                    )
                elif (
                    ConsensusExperiment.model_validate(
                        experiment_row.payload
                    )
                    != experiment
                ):
                    raise ValidationError(
                        "Immutable consensus experiment identity conflict"
                    )
                event_row = await session.get(
                    ConsensusExperimentEventModel,
                    created_event.event_id,
                )
                if event_row is None:
                    session.add(
                        ConsensusExperimentEventModel(
                            event_id=created_event.event_id,
                            schema_version=created_event.schema_version,
                            experiment_id=created_event.experiment_id,
                            event_type=created_event.event_type,
                            actor=created_event.actor,
                            payload=event_payload,
                            created_at=created_event.created_at,
                        )
                    )
                elif (
                    ConsensusExperimentEvent.model_validate(
                        event_row.payload
                    )
                    != created_event
                ):
                    raise ValidationError(
                        "Immutable consensus experiment event conflict"
                    )
            return experiment, created_event
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist experiment definition: {exc}"
            ) from exc

    async def list_consensus_experiments(
        self,
        *,
        limit: int = 100,
    ) -> list[ConsensusExperiment]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Consensus experiment limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(ConsensusExperimentModel)
                    .order_by(
                        ConsensusExperimentModel.created_at.desc(),
                        ConsensusExperimentModel.experiment_id,
                    )
                    .limit(limit)
                )
            )
            return [
                ConsensusExperiment.model_validate(row.payload)
                for row in rows
            ]

    async def save_consensus_experiment_event(
        self,
        event: ConsensusExperimentEvent,
    ) -> ConsensusExperimentEvent:
        payload = event.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=ConsensusExperimentEventModel,
            identity=event.event_id,
            payload=payload,
            values={
                "event_id": event.event_id,
                "schema_version": event.schema_version,
                "experiment_id": event.experiment_id,
                "event_type": event.event_type,
                "actor": event.actor,
                "payload": payload,
                "created_at": event.created_at,
            },
            contract_class=ConsensusExperimentEvent,
            label="consensus experiment event",
        )

    async def list_consensus_experiment_events(
        self,
        *,
        limit: int = 100,
    ) -> list[ConsensusExperimentEvent]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Consensus event limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(ConsensusExperimentEventModel)
                    .order_by(
                        ConsensusExperimentEventModel.created_at.desc(),
                        ConsensusExperimentEventModel.event_id,
                    )
                    .limit(limit)
                )
            )
            return [
                ConsensusExperimentEvent.model_validate(row.payload)
                for row in rows
            ]

    async def save_weighted_consensus(
        self,
        consensus: WeightedConsensus,
    ) -> WeightedConsensus:
        payload = consensus.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=WeightedConsensusModel,
            identity=consensus.consensus_id,
            payload=payload,
            values={
                "consensus_id": consensus.consensus_id,
                "schema_version": consensus.schema_version,
                "correlation_id": consensus.correlation_id,
                "experiment_id": consensus.experiment_id,
                "symbol": consensus.symbol,
                "timeframe": consensus.timeframe,
                "status": consensus.status,
                "eligible_agent_count": consensus.eligible_agent_count,
                "payload": payload,
                "created_at": consensus.created_at,
            },
            contract_class=WeightedConsensus,
            label="weighted consensus",
        )

    async def list_weighted_consensus(
        self,
        *,
        limit: int = 100,
    ) -> list[WeightedConsensus]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Weighted consensus limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(WeightedConsensusModel)
                    .order_by(
                        WeightedConsensusModel.created_at.desc(),
                        WeightedConsensusModel.consensus_id,
                    )
                    .limit(limit)
                )
            )
            return [
                WeightedConsensus.model_validate(row.payload)
                for row in rows
            ]

    async def save_drift_observation(
        self,
        observation: DriftObservation,
    ) -> DriftObservation:
        payload = observation.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=DriftObservationModel,
            identity=observation.observation_id,
            payload=payload,
            values={
                "observation_id": observation.observation_id,
                "schema_version": observation.schema_version,
                "experiment_id": observation.experiment_id,
                "agent_name": observation.agent_name,
                "agent_version": observation.agent_version,
                "severity": observation.severity,
                "payload": payload,
                "observed_at": observation.observed_at,
                "created_at": observation.created_at,
            },
            contract_class=DriftObservation,
            label="drift observation",
        )

    async def save_drift_observations(
        self,
        observations: list[DriftObservation],
    ) -> list[DriftObservation]:
        """Append one drift cohort atomically to avoid per-agent commits."""

        if not observations:
            return []
        try:
            stored: list[DriftObservation] = []
            async with self._db.session() as session, session.begin():
                for observation in observations:
                    existing = await session.get(
                        DriftObservationModel,
                        observation.observation_id,
                    )
                    if existing is not None:
                        current = DriftObservation.model_validate(
                            existing.payload
                        )
                        if current != observation:
                            raise ValidationError(
                                "Immutable drift observation identity conflict"
                            )
                        stored.append(current)
                        continue
                    payload = observation.model_dump(mode="json")
                    session.add(
                        DriftObservationModel(
                            observation_id=observation.observation_id,
                            schema_version=observation.schema_version,
                            experiment_id=observation.experiment_id,
                            agent_name=observation.agent_name,
                            agent_version=observation.agent_version,
                            severity=observation.severity,
                            payload=payload,
                            observed_at=observation.observed_at,
                            created_at=observation.created_at,
                        )
                    )
                    stored.append(observation)
            return stored
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist drift observation cohort: {exc}"
            ) from exc

    async def list_drift_observations(
        self,
        *,
        limit: int = 100,
    ) -> list[DriftObservation]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Drift observation limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(DriftObservationModel)
                    .order_by(
                        DriftObservationModel.observed_at.desc(),
                        DriftObservationModel.observation_id,
                    )
                    .limit(limit)
                )
            )
            return [
                DriftObservation.model_validate(row.payload)
                for row in rows
            ]

    async def save_portfolio_proposal(
        self,
        proposal: PortfolioProposal,
    ) -> PortfolioProposal:
        payload = proposal.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=PortfolioProposalModel,
            identity=proposal.proposal_id,
            payload=payload,
            values={
                "proposal_id": proposal.proposal_id,
                "schema_version": proposal.schema_version,
                "correlation_id": proposal.correlation_id,
                "consensus_id": proposal.consensus_id,
                "experiment_id": proposal.experiment_id,
                "symbol": proposal.symbol,
                "timeframe": proposal.timeframe,
                "status": proposal.status,
                "max_notional": proposal.max_notional,
                "payload": payload,
                "created_at": proposal.created_at,
            },
            contract_class=PortfolioProposal,
            label="portfolio proposal",
        )

    async def list_portfolio_proposals(
        self,
        *,
        limit: int = 100,
    ) -> list[PortfolioProposal]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Portfolio proposal limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(PortfolioProposalModel)
                    .order_by(
                        PortfolioProposalModel.created_at.desc(),
                        PortfolioProposalModel.proposal_id,
                    )
                    .limit(limit)
                )
            )
            return [
                PortfolioProposal.model_validate(row.payload)
                for row in rows
            ]

    async def save_operational_metric_snapshot(
        self,
        snapshot: OperationalMetricSnapshot,
    ) -> OperationalMetricSnapshot:
        payload = snapshot.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=OperationalMetricSnapshotModel,
            identity=snapshot.snapshot_id,
            payload=payload,
            values={
                "snapshot_id": snapshot.snapshot_id,
                "schema_version": snapshot.schema_version,
                "correlation_id": snapshot.correlation_id,
                "registered_agents": snapshot.registered_agents,
                "active_agents": snapshot.active_agents,
                "payload": payload,
                "captured_at": snapshot.captured_at,
            },
            contract_class=OperationalMetricSnapshot,
            label="operational metric snapshot",
        )

    async def list_operational_metric_snapshots(
        self,
        *,
        limit: int = 100,
    ) -> list[OperationalMetricSnapshot]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Operational snapshot limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(OperationalMetricSnapshotModel)
                    .order_by(
                        OperationalMetricSnapshotModel.captured_at.desc(),
                        OperationalMetricSnapshotModel.snapshot_id,
                    )
                    .limit(limit)
                )
            )
            return [
                OperationalMetricSnapshot.model_validate(row.payload)
                for row in rows
            ]

    async def save_slo_evaluations(
        self,
        evaluations: list[SLOEvaluation],
    ) -> list[SLOEvaluation]:
        if not evaluations:
            return []
        try:
            stored: list[SLOEvaluation] = []
            async with self._db.session() as session, session.begin():
                for evaluation in evaluations:
                    existing = await session.get(
                        SLOEvaluationModel,
                        evaluation.evaluation_id,
                    )
                    if existing is not None:
                        current = SLOEvaluation.model_validate(
                            existing.payload
                        )
                        if current != evaluation:
                            raise ValidationError(
                                "Immutable SLO evaluation identity conflict"
                            )
                        stored.append(current)
                        continue
                    payload = evaluation.model_dump(mode="json")
                    session.add(
                        SLOEvaluationModel(
                            evaluation_id=evaluation.evaluation_id,
                            schema_version=evaluation.schema_version,
                            slo_name=evaluation.slo_name,
                            status=evaluation.status,
                            sample_count=evaluation.sample_count,
                            payload=payload,
                            evaluated_at=evaluation.evaluated_at,
                        )
                    )
                    stored.append(evaluation)
            return stored
        except (DatabaseError, ValidationError):
            raise
        except Exception as exc:
            raise DatabaseError(
                f"Failed to persist SLO evaluations: {exc}"
            ) from exc

    async def list_slo_evaluations(
        self,
        *,
        limit: int = 100,
    ) -> list[SLOEvaluation]:
        if not 1 <= limit <= 100_000:
            raise ValueError("SLO evaluation limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(SLOEvaluationModel)
                    .order_by(
                        SLOEvaluationModel.evaluated_at.desc(),
                        SLOEvaluationModel.evaluation_id,
                    )
                    .limit(limit)
                )
            )
            return [
                SLOEvaluation.model_validate(row.payload)
                for row in rows
            ]

    async def save_operational_alert_event(
        self,
        event: OperationalAlertEvent,
    ) -> OperationalAlertEvent:
        payload = event.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=OperationalAlertEventModel,
            identity=event.alert_event_id,
            payload=payload,
            values={
                "alert_event_id": event.alert_event_id,
                "schema_version": event.schema_version,
                "alert_key": event.alert_key,
                "lifecycle_sequence": event.lifecycle_sequence,
                "event_type": event.event_type,
                "severity": event.severity,
                "payload": payload,
                "occurred_at": event.occurred_at,
            },
            contract_class=OperationalAlertEvent,
            label="operational alert event",
        )

    async def list_operational_alert_events(
        self,
        *,
        limit: int = 100,
    ) -> list[OperationalAlertEvent]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Operational alert limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(OperationalAlertEventModel)
                    .order_by(
                        OperationalAlertEventModel.occurred_at.desc(),
                        OperationalAlertEventModel.alert_event_id,
                    )
                    .limit(limit)
                )
            )
            return [
                OperationalAlertEvent.model_validate(row.payload)
                for row in rows
            ]

    async def save_cost_usage_record(
        self,
        record: CostUsageRecord,
    ) -> CostUsageRecord:
        payload = record.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=CostUsageRecordModel,
            identity=record.usage_id,
            payload=payload,
            values={
                "usage_id": record.usage_id,
                "schema_version": record.schema_version,
                "cost_center": record.cost_center,
                "resource": record.resource,
                "estimated_cost_usd": record.estimated_cost_usd,
                "payload": payload,
                "observed_at": record.observed_at,
            },
            contract_class=CostUsageRecord,
            label="cost usage record",
        )

    async def list_cost_usage_records(
        self,
        *,
        limit: int = 100,
    ) -> list[CostUsageRecord]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Cost usage limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(CostUsageRecordModel)
                    .order_by(
                        CostUsageRecordModel.observed_at.desc(),
                        CostUsageRecordModel.usage_id,
                    )
                    .limit(limit)
                )
            )
            return [
                CostUsageRecord.model_validate(row.payload)
                for row in rows
            ]

    async def save_resilience_test_run(
        self,
        run: ResilienceTestRun,
    ) -> ResilienceTestRun:
        payload = run.model_dump(mode="json")
        return await self._save_governance_artifact(
            model_class=ResilienceTestRunModel,
            identity=run.run_id,
            payload=payload,
            values={
                "run_id": run.run_id,
                "schema_version": run.schema_version,
                "run_type": run.run_type,
                "scenario": run.scenario,
                "status": run.status,
                "payload": payload,
                "completed_at": run.completed_at,
            },
            contract_class=ResilienceTestRun,
            label="resilience test run",
        )

    async def list_resilience_test_runs(
        self,
        *,
        limit: int = 100,
    ) -> list[ResilienceTestRun]:
        if not 1 <= limit <= 100_000:
            raise ValueError("Resilience run limit must be 1..100000")
        async with self._db.session() as session:
            rows = list(
                await session.scalars(
                    select(ResilienceTestRunModel)
                    .order_by(
                        ResilienceTestRunModel.completed_at.desc(),
                        ResilienceTestRunModel.run_id,
                    )
                    .limit(limit)
                )
            )
            return [
                ResilienceTestRun.model_validate(row.payload)
                for row in rows
            ]

    async def get_decisions(self, limit: int = 50) -> list[DecisionModel]:
        async with self._db.session() as session:
            result = await session.execute(
                select(DecisionModel).order_by(DecisionModel.created_at.desc()).limit(limit)
            )
            return list(result.scalars())

    async def get_decision(self, decision_id: str) -> DecisionModel | None:
        async with self._db.session() as session:
            return await session.get(DecisionModel, decision_id)

    async def get_audit_by_correlation(self, correlation_id: str) -> list[AuditLogModel]:
        async with self._db.session() as session:
            result = await session.execute(
                select(AuditLogModel)
                .where(AuditLogModel.correlation_id == correlation_id)
                .order_by(AuditLogModel.created_at)
            )
            return list(result.scalars())

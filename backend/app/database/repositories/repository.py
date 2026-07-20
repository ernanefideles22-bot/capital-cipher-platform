"""Persistence repository for the decision chain (docs/12).

Critical rule: if a decision or risk check cannot be recorded, the operation
must not advance (enforced by callers via raised DatabaseError).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.errors import DatabaseError, ValidationError
from app.backtesting.artifacts import walk_forward_artifact_hash
from app.database.models import (
    AgentExecutionAttemptModel,
    AgentExecutionJobModel,
    AgentMemoryEntryModel,
    AgentOutputModel,
    AuditLogModel,
    BackfillQueueItemModel,
    BackfillRawPageModel,
    CandleObservationModel,
    ClockObservationModel,
    DecisionModel,
    DatasetManifestModel,
    EventJournalModel,
    EventOutboxModel,
    HistoricalBackfillJobModel,
    MarketDataGapModel,
    PaperOrderModel,
    RawMarketEventModel,
    RawDataObjectModel,
    ReplayCheckpointModel,
    RiskControlEventModel,
    RiskControlStateModel,
    RiskEvaluationModel,
    RiskCheckModel,
    OrderApprovalModel,
    SystemEventModel,
    WalkForwardExperimentModel,
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
from app.schemas.common import AgentStatus, OrderSide, PaperOrderStatus
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
from app.schemas.risk import (
    ApprovalStatus,
    OrderApproval,
    PositionExposure,
    RiskCheck,
    RiskControlState,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _position_snapshot_hash(rows: list[PaperOrderModel]) -> str:
    payload = {
        "positions": sorted(
            (
                {
                    "paper_order_id": row.id,
                    "symbol": row.symbol,
                    "timeframe": row.timeframe or "unknown",
                    "strategy": row.strategy,
                    "side": row.side,
                    "notional": round(float(row.position_size), 8),
                    "leverage": round(float(row.leverage), 8),
                }
                for row in rows
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


class Repository:
    def __init__(self, database: Database) -> None:
        self._db = database

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
    ) -> None:
        """Lock, consume and insert the PAPER order atomically."""

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
                open_rows = list(
                    await session.scalars(
                        select(PaperOrderModel)
                        .where(
                            PaperOrderModel.status
                            == PaperOrderStatus.FILLED.value
                        )
                        .with_for_update()
                    )
                )
                if stored.position_snapshot_hash != _position_snapshot_hash(
                    open_rows
                ):
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
                session.add(PaperOrderModel(**self._paper_order_values(order)))
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
                rows = list(
                    await session.scalars(
                        select(PaperOrderModel).where(
                            PaperOrderModel.status
                            == PaperOrderStatus.FILLED.value
                        )
                    )
                )
                return [
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
        except Exception as exc:
            raise DatabaseError(
                f"Failed to restore open risk exposure: {exc}"
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

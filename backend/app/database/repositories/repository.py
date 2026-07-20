"""Persistence repository for the decision chain (docs/12).

Critical rule: if a decision or risk check cannot be recorded, the operation
must not advance (enforced by callers via raised DatabaseError).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.errors import DatabaseError
from app.database.models import (
    AgentOutputModel,
    AuditLogModel,
    DecisionModel,
    EventJournalModel,
    EventOutboxModel,
    MarketCandleModel,
    PaperOrderModel,
    RawMarketEventModel,
    ReplayCheckpointModel,
    RiskCheckModel,
    SystemEventModel,
)
from app.database.session import Database
from app.schemas.agents import AgentOutput
from app.schemas.decisions import Decision
from app.schemas.events import BusMessage
from app.schemas.replay import ReplayCheckpoint
from app.schemas.market import Candle, RawMarketEvent
from app.schemas.paper import PaperOrder
from app.schemas.risk import RiskCheck


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    async def save_candle(self, candle: Candle) -> None:
        try:
            async with self._db.session() as session, session.begin():
                session.add(
                    MarketCandleModel(
                        exchange=candle.exchange.value,
                        symbol=candle.symbol,
                        timeframe=candle.timeframe,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        closed_at=candle.closed_at,
                        created_at=_now(),
                    )
                )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist candle: {exc}") from exc

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
                    session.add(
                        PaperOrderModel(
                            id=order.paper_order_id,
                            decision_id=order.decision_id,
                            risk_check_id=order.risk_check_id,
                            correlation_id=order.correlation_id,
                            exchange=order.exchange.value,
                            symbol=order.symbol,
                            side=order.side.value,
                            entry_price=order.entry_price,
                            stop_loss=order.stop_loss,
                            take_profit=order.take_profit,
                            position_size=order.position_size,
                            status=order.status.value,
                            fees_estimated=order.fees_estimated,
                            slippage_estimated=order.slippage_estimated,
                            opened_at=order.opened_at,
                            closed_at=order.closed_at,
                            pnl=order.pnl,
                            created_at=order.created_at,
                        )
                    )
        except Exception as exc:
            raise DatabaseError(f"Failed to persist paper order: {exc}") from exc

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

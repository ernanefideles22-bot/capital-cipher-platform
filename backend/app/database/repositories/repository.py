"""Persistence repository for the decision chain (docs/12).

Critical rule: if a decision or risk check cannot be recorded, the operation
must not advance (enforced by callers via raised DatabaseError).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.errors import DatabaseError
from app.database.models import (
    AgentOutputModel,
    AuditLogModel,
    DecisionModel,
    MarketCandleModel,
    PaperOrderModel,
    RiskCheckModel,
    SystemEventModel,
)
from app.database.session import Database
from app.schemas.agents import AgentOutput
from app.schemas.decisions import Decision
from app.schemas.market import Candle
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

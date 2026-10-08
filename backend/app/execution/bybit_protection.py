"""Durable, fail-closed Bybit TESTNET order protection lookup."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import text

from app.core.errors import SecurityError
from app.database.session import Database


@dataclass(frozen=True)
class BybitOrderProtection:
    oms_order_id: str
    approval_id: str
    exchange: str
    environment: str
    execution_intent: str
    reduce_only: bool
    stop_loss: float
    take_profit: float
    leverage: float


class BybitOrderProtectionStore:
    """Read immutable protection captured atomically with the OMS order."""

    def __init__(self, database_url: str) -> None:
        self._database = Database(
            database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout_seconds=10,
            pool_recycle_seconds=300,
        )

    @classmethod
    def from_environment(cls) -> "BybitOrderProtectionStore":
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise SecurityError(
                "Bybit TESTNET protection store requires DATABASE_URL"
            )
        if database_url.startswith("postgresql://"):
            database_url = "postgresql+asyncpg://" + database_url.removeprefix(
                "postgresql://"
            )
        if not database_url.startswith("postgresql+asyncpg://"):
            raise SecurityError(
                "Bybit TESTNET protection store requires PostgreSQL"
            )
        return cls(database_url)

    async def load(self, oms_order_id: str) -> BybitOrderProtection | None:
        query = text(
            "SELECT oms_order_id, approval_id, exchange, environment, "
            "execution_intent, reduce_only, stop_loss, take_profit, leverage "
            "FROM capital_cipher.oms_order_protections "
            "WHERE oms_order_id = :oms_order_id"
        )
        async with self._database.engine.connect() as connection:
            row = (
                await connection.execute(query, {"oms_order_id": oms_order_id})
            ).mappings().one_or_none()
        if row is None:
            return None
        return BybitOrderProtection(
            oms_order_id=row["oms_order_id"],
            approval_id=row["approval_id"],
            exchange=row["exchange"],
            environment=row["environment"],
            execution_intent=row["execution_intent"],
            reduce_only=bool(row["reduce_only"]),
            stop_loss=float(row["stop_loss"]),
            take_profit=float(row["take_profit"]),
            leverage=float(row["leverage"]),
        )

    async def aclose(self) -> None:
        await self._database.engine.dispose()

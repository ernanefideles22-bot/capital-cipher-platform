"""Read and validate Gate protection captured by the central OMS transaction."""
import os

from sqlalchemy import text

from app.core.errors import SecurityError
from app.database.session import Database
from app.execution.gate_protection_validation import validate_gate_protection


class GateProtectionStore:
    def __init__(self, database):
        self._database=database

    @classmethod
    def from_environment(cls):
        url=os.environ.get("DATABASE_URL","").strip()
        if url.startswith("postgresql://"):
            url="postgresql+asyncpg://"+url.removeprefix("postgresql://")
        if not url.startswith("postgresql+asyncpg://"):
            raise SecurityError("Gate protection requires PostgreSQL")
        return cls(Database(url,pool_size=1,max_overflow=0,pool_timeout_seconds=10,pool_recycle_seconds=300))

    async def verify(self, order):
        try:
            # Compare the original NUMERIC values exactly in PostgreSQL before
            # projecting into the same float representation used by OMSOrder.
            # No tick-size or price-distance tolerance is used.
            query=text("SELECT p.oms_order_id,p.approval_id,p.exchange,p.environment,p.symbol,p.side,"
                       "p.execution_intent,p.reduce_only,p.stop_loss,p.take_profit,p.leverage,"
                       "CAST(p.reference_price AS DOUBLE PRECISION) AS reference_price "
                       "FROM capital_cipher.oms_order_protections p "
                       "JOIN capital_cipher.oms_orders o ON o.oms_order_id=p.oms_order_id "
                       "AND o.approval_id=p.approval_id "
                       "WHERE p.oms_order_id=:id AND p.reference_price=o.reference_price "
                       "AND p.leverage=o.leverage")
            async with self._database.engine.connect() as connection:
                row=(await connection.execute(query,{"id":order.oms_order_id})).mappings().one_or_none()
            return validate_gate_protection(order,row)
        except Exception:
            raise SecurityError("Gate durable protection unavailable or invalid") from None

    async def aclose(self):
        await self._database.dispose()

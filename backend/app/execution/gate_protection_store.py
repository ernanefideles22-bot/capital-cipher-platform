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
            query=text("SELECT oms_order_id,approval_id,exchange,environment,symbol,side,"
                       "execution_intent,reduce_only,stop_loss,take_profit,leverage,reference_price "
                       "FROM capital_cipher.oms_order_protections WHERE oms_order_id=:id")
            async with self._database.engine.connect() as connection:
                row=(await connection.execute(query,{"id":order.oms_order_id})).mappings().one_or_none()
            return validate_gate_protection(order,row)
        except Exception:
            raise SecurityError("Gate durable protection unavailable or invalid") from None

    async def aclose(self):
        await self._database.dispose()

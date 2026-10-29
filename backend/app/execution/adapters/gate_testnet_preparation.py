"""Fail-closed Gate OMS preparation; no order or exchange I/O is implemented."""
from app.core.errors import SecurityError
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.execution.adapters.gate_order_constraints import gate_client_id, prepare_contract_quantity
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment, OMSOrderType, OMSTimeInForce


class GateTestnetPreparationAdapter(ExchangeExecutionAdapter):
    """Prepare OMS identities and sizes while all execution remains disabled.

    The metadata provider is read-only. This class must not be registered as a
    usable exchange adapter until protection, reconciliation and OMS lifecycle
    are independently implemented and tested against Gate Testnet.
    """
    exchange = Exchange.GATE

    def __init__(self, metadata_provider, clock):
        if not callable(metadata_provider) or not callable(clock):
            raise ValueError("read-only metadata provider and clock required")
        self._metadata_provider=metadata_provider
        self._clock=clock

    async def prepare_order(self, order):
        if (order.exchange != Exchange.GATE or order.environment != ExecutionEnvironment.TESTNET
                or order.order_type != OMSOrderType.MARKET or order.time_in_force != OMSTimeInForce.IOC):
            raise SecurityError("Gate preparation requires scoped TESTNET market IOC order")
        try:
            metadata, observed_at = await self._metadata_provider(order.symbol)
            prepared=prepare_contract_quantity(
                symbol=order.symbol,side=order.side.value,requested_quantity=order.quantity,
                max_notional=order.requested_notional,reference_price=order.reference_price,
                metadata=metadata,observed_at=observed_at,now=self._clock())
            client_id=gate_client_id(order.approval_id)
            return order.model_copy(update={"client_order_id":client_id,
                "quantity":float(prepared["base_quantity"])})
        except Exception:
            raise SecurityError("Gate contract preparation failed") from None

    async def healthcheck(self):
        return False

    async def submit_order(self, order):
        raise SecurityError("Gate execution and stop protection are not enabled")

    async def cancel_order(self, order):
        raise SecurityError("Gate execution and reconciliation are not enabled")

    async def fetch_state(self, *, symbols=None):
        raise SecurityError("Gate OMS venue reconciliation is not available")

    async def aclose(self):
        return None

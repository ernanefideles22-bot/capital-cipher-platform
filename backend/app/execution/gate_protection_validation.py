"""Read-only validation of persisted Gate TESTNET protection evidence."""
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping

from app.core.errors import SecurityError
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment


def validate_gate_protection(order, row):
    """Verify a durable row against its OMS order; never authorize submission.

    The caller must obtain the row from the immutable protection table, verify
    current exchange state and clock, and separately satisfy central controls.
    """
    try:
        if not isinstance(row,Mapping):
            raise ValueError()
        if order.exchange != Exchange.GATE or order.environment != ExecutionEnvironment.TESTNET:
            raise ValueError()
        if (row["oms_order_id"] != order.oms_order_id or row["approval_id"] != order.approval_id
                or row["exchange"] != "GATE" or row["environment"] != "TESTNET"
                or row["symbol"] != order.symbol or row["side"] != order.side.value
                or row["execution_intent"] != "ENTRY" or row["reduce_only"] is not False):
            raise ValueError()
        raw=(row["stop_loss"],row["take_profit"],row["leverage"],row["reference_price"],
             order.leverage,order.reference_price)
        if any(x is None or isinstance(x,bool) for x in raw):
            raise ValueError()
        stop,target,leverage,reference,order_leverage,order_reference=(Decimal(str(x)) for x in raw)
        if any(not x.is_finite() or x <= 0 for x in (stop,target,leverage,reference,order_leverage,order_reference)):
            raise ValueError()
        if leverage != 1 or order_leverage != 1 or reference != order_reference:
            raise ValueError()
        if order.side.value == "BUY":
            if not stop < reference < target:
                raise ValueError()
        elif order.side.value == "SELL":
            if not target < reference < stop:
                raise ValueError()
        else:
            raise ValueError()
        return {"verified":True,"execution_authorized":False,"stop_loss":str(stop),"take_profit":str(target)}
    except (KeyError,TypeError,ValueError,InvalidOperation,AttributeError):
        raise SecurityError("Gate durable protection evidence invalid") from None

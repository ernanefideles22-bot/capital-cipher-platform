import pytest

from app.core.errors import SecurityError
from app.execution.gate_protection_validation import validate_gate_protection
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment, OMSOrder


def order():
    return OMSOrder(oms_order_id="order-1",client_order_id="t-example",decision_id="decision",
        risk_check_id="risk",approval_id="a"*64,request_fingerprint="b"*64,correlation_id="corr",
        exchange=Exchange.GATE,environment=ExecutionEnvironment.TESTNET,symbol="BTCUSDT",
        timeframe="15m",strategy="TEST",side=OrderSide.BUY,quantity=0.001,
        requested_notional=100,reference_price=100000,leverage=1)


def evidence():
    return {"oms_order_id":"order-1","approval_id":"a"*64,"exchange":"GATE",
            "environment":"TESTNET","symbol":"BTCUSDT","side":"BUY","execution_intent":"ENTRY",
            "reduce_only":False,"stop_loss":"98000","take_profit":"104000","leverage":"1",
            "reference_price":"100000"}


def test_valid_protection_is_verified_but_does_not_authorize_execution():
    result=validate_gate_protection(order(),evidence())
    assert result["verified"] and result["execution_authorized"] is False


@pytest.mark.parametrize("field,value", [
    ("oms_order_id","another"),("approval_id","b"*64),("exchange","BYBIT"),
    ("environment","PAPER"),("symbol","ETHUSDT"),("side","SELL"),("execution_intent","EXIT"),
    ("reduce_only",True),("stop_loss","100001"),("take_profit","99999"),
    ("leverage","76"),("reference_price","99999"),("stop_loss","NaN"),("stop_loss",True),
])
def test_mismatched_or_invalid_protection_blocks(field,value):
    row=evidence()
    row[field]=value
    with pytest.raises(SecurityError): validate_gate_protection(order(),row)


def test_missing_or_non_gate_evidence_blocks():
    row=evidence()
    del row["stop_loss"]
    with pytest.raises(SecurityError): validate_gate_protection(order(),row)
    with pytest.raises(SecurityError): validate_gate_protection(order().model_copy(update={"exchange":Exchange.BYBIT}),evidence())

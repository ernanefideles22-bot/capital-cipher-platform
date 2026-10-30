from types import SimpleNamespace
import pytest

from app.core.errors import SecurityError
from app.execution.gate_protection_store import GateProtectionStore
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment, OMSOrder


def order():
    return OMSOrder(oms_order_id="order-1",client_order_id="t-example",decision_id="decision",
        risk_check_id="risk",approval_id="a"*64,request_fingerprint="b"*64,correlation_id="corr",
        exchange=Exchange.GATE,environment=ExecutionEnvironment.TESTNET,symbol="BTCUSDT",
        timeframe="15m",strategy="TEST",side=OrderSide.BUY,quantity=0.001,
        requested_notional=100,reference_price=100000,leverage=1)


def row():
    return {"oms_order_id":"order-1","approval_id":"a"*64,"exchange":"GATE",
            "environment":"TESTNET","symbol":"BTCUSDT","side":"BUY","execution_intent":"ENTRY",
            "reduce_only":False,"stop_loss":"98000","take_profit":"104000","leverage":"1",
            "reference_price":"100000"}


class Connection:
    def __init__(self, value): self.value=value
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return None
    async def execute(self, query, parameters):
        assert "capital_cipher.oms_order_protections" in str(query)
        assert parameters=={"id":"order-1"}
        if isinstance(self.value,Exception): raise self.value
        return SimpleNamespace(mappings=lambda:SimpleNamespace(one_or_none=lambda:self.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("value,valid",[(row(),True),(None,False),(RuntimeError("secret details"),False)])
async def test_read_only_lookup_validates_and_sanitizes(value,valid):
    db=SimpleNamespace(engine=SimpleNamespace(connect=lambda:Connection(value)))
    store=GateProtectionStore(db)
    if valid:
        result=await store.verify(order())
        assert result["verified"] and result["execution_authorized"] is False
    else:
        with pytest.raises(SecurityError) as exc: await store.verify(order())
        assert "secret details" not in str(exc.value)


def test_runtime_store_refuses_non_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","sqlite:///fixture.db")
    with pytest.raises(SecurityError): GateProtectionStore.from_environment()

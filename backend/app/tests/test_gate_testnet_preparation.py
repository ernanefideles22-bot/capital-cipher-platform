import pytest

from app.core.errors import SecurityError
from app.execution.adapters.gate_testnet_preparation import GateTestnetPreparationAdapter
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment, OMSOrder, OMSOrderStatus


def order():
    return OMSOrder(oms_order_id="order-1",client_order_id="cc-example",decision_id="decision",
        risk_check_id="risk",approval_id="a"*64,request_fingerprint="b"*64,correlation_id="corr",
        exchange=Exchange.GATE,environment=ExecutionEnvironment.TESTNET,symbol="BTCUSDT",
        timeframe="15m",strategy="TEST",side=OrderSide.BUY,quantity=0.00025,
        requested_notional=20,reference_price=80000,status=OMSOrderStatus.PENDING_SUBMISSION)


async def metadata(_symbol):
    return ({"name":"BTC_USDT","quanto_multiplier":"0.0001","order_size_min":"1",
             "order_size_max":"100","in_delisting":False},1000)


@pytest.mark.asyncio
async def test_prepares_quantity_and_identity_but_disallows_all_execution():
    adapter=GateTestnetPreparationAdapter(metadata,lambda:1001)
    prepared=await adapter.prepare_order(order())
    assert prepared.quantity==0.0002 and prepared.client_order_id.startswith("t-")
    assert len(prepared.client_order_id)==30
    assert not await adapter.healthcheck()
    for call in (adapter.submit_order(prepared),adapter.cancel_order(prepared),adapter.fetch_state()):
        with pytest.raises(SecurityError):
            await call
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rejects_wrong_venue_and_stale_metadata():
    adapter=GateTestnetPreparationAdapter(metadata,lambda:1011)
    with pytest.raises(SecurityError): await adapter.prepare_order(order())
    wrong=order().model_copy(update={"exchange":Exchange.BYBIT})
    with pytest.raises(SecurityError): await adapter.prepare_order(wrong)

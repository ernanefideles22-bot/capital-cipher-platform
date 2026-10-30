import httpx
import pytest
from app.execution.adapters.gate_testnet_metadata import GateTestnetMetadata, GateMetadataUnavailable
from app.execution.adapters.gate_testnet_preparation import GateTestnetPreparationAdapter
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import ExecutionEnvironment, OMSOrder, OMSOrderStatus


def payload():
    return {"name":"BTC_USDT","quanto_multiplier":"0.0001","order_size_min":"1",
            "order_size_max":"100","in_delisting":False,"extraneous":"not retained"}


def order():
    return OMSOrder(oms_order_id="order-1",client_order_id="cc-example",decision_id="decision",
        risk_check_id="risk",approval_id="a"*64,request_fingerprint="b"*64,correlation_id="corr",
        exchange=Exchange.GATE,environment=ExecutionEnvironment.TESTNET,symbol="BTCUSDT",
        timeframe="15m",strategy="TEST",side=OrderSide.BUY,quantity=0.00025,
        requested_notional=20,reference_price=80000,status=OMSOrderStatus.PENDING_SUBMISSION)


@pytest.mark.asyncio
async def test_public_testnet_metadata_to_oms_preparation():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=="GET" and request.url.host=="api-testnet.gateapi.io"
        assert request.url.path=="/api/v4/futures/usdt/contracts/BTC_USDT"
        assert "KEY" not in request.headers
        return httpx.Response(200,json=payload())
    provider=GateTestnetMetadata(transport=httpx.MockTransport(handler),clock=lambda:1000)
    adapter=GateTestnetPreparationAdapter(provider,lambda:1001)
    prepared=await adapter.prepare_order(order())
    assert prepared.quantity==0.0002 and prepared.client_order_id.startswith("t-")
    assert not await adapter.healthcheck() and len(calls)==1
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["redirect","error","mismatch","missing","badjson","toolarge"])
async def test_metadata_rejects_wrong_or_unavailable_responses(kind):
    def handler(_request):
        if kind=="redirect": return httpx.Response(302,headers={"location":"https://example.com"})
        if kind=="error": return httpx.Response(503)
        if kind=="badjson": return httpx.Response(200,text="no")
        if kind=="toolarge": return httpx.Response(200,text="a"*65537)
        data=payload()
        if kind=="mismatch": data["name"]="ETH_USDT"
        if kind=="missing": del data["order_size_max"]
        return httpx.Response(200,json=data)
    provider=GateTestnetMetadata(transport=httpx.MockTransport(handler),clock=lambda:1000)
    with pytest.raises(GateMetadataUnavailable): await provider("BTCUSDT")
    await provider.aclose()


@pytest.mark.asyncio
async def test_unknown_symbol_never_reaches_network():
    calls=[]
    provider=GateTestnetMetadata(transport=httpx.MockTransport(lambda req:calls.append(req)),clock=lambda:1000)
    with pytest.raises(GateMetadataUnavailable): await provider("BTC_USDT/../../")
    assert not calls
    await provider.aclose()


@pytest.mark.asyncio
async def test_tick_size_is_retained_for_exact_stop_validation():
    data=payload(); data['order_price_round']='0.1'
    provider=GateTestnetMetadata(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=data)),clock=lambda:1000)
    result,_=await provider('BTCUSDT')
    assert result['order_price_round']=='0.1' and 'extraneous' not in result
    await provider.aclose()

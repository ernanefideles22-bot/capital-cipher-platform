import hashlib
import hmac
import httpx
import pytest
from app.demo.gate_stop_probe import confirm_stop


def row():
    return {'id_string':'123','status':'open','order_type':'close-long-position','create_time':1000,
            'initial':{'contract':'BTC_USDT','size':0,'price':'0','tif':'ioc','is_reduce_only':True,'is_close':True},
            'trigger':{'strategy_type':0,'price_type':1,'price':'79000','rule':2,'expiration':3600}}


async def no_sleep(_):
    pass


@pytest.mark.asyncio
async def test_waits_for_stop_propagation_with_read_only_bounded_requests():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET' and request.url.host=='api-testnet.gateapi.io'
        assert request.url.path=='/api/v4/futures/usdt/price_orders/123'
        digest=hashlib.sha512(b'').hexdigest()
        message=f'GET\n{request.url.path}\n\n{digest}\n1010'
        assert request.headers['SIGN']==hmac.new(b'secret',message.encode(),hashlib.sha512).hexdigest()
        return httpx.Response(404,json={}) if len(calls)<3 else httpx.Response(200,json=row())
    assert await confirm_stop('key','secret',order_id='123',contract='BTC_USDT',position_size=2,
         trigger_price='79000',transport=httpx.MockTransport(handler),clock=lambda:1010,sleep=no_sleep)
    assert len(calls)==3


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[{}, {'id':123}, {**row(),'status':'finished'}])
async def test_never_accepts_unverified_or_finished_stop(payload):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=payload)
    assert not await confirm_stop('key','secret',order_id='123',contract='BTC_USDT',position_size=2,
        trigger_price='79000',transport=httpx.MockTransport(handler),clock=lambda:1010,sleep=no_sleep)
    assert len(calls)==3


@pytest.mark.asyncio
async def test_blocks_redirects_and_invalid_order_ids():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':'https://other.example'})
    transport=httpx.MockTransport(handler)
    assert not await confirm_stop('key','secret',order_id='../1',contract='BTC_USDT',position_size=2,
                                  trigger_price='79000',transport=transport,clock=lambda:1010,sleep=no_sleep)
    assert not calls
    assert not await confirm_stop('key','secret',order_id='123',contract='BTC_USDT',position_size=2,
                                  trigger_price='79000',transport=transport,clock=lambda:1010,sleep=no_sleep)
    assert len(calls)==3

import hashlib
import hmac
import json
import httpx
import pytest

from app.demo.gate_testnet_transport import GateTestnetTransport, TestnetCredentials as Credentials, GateWriteDisabled, GateWriteUnknown, GateReadError


def client(handler, *, enabled=False, ack=''):
    return GateTestnetTransport(Credentials('dummy-key','dummy-secret'),trusted_clock=lambda:1000,
                                writes_enabled=enabled,acknowledgement=ack,transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_default_disabled_and_no_ack_produce_no_network_calls():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(201,json={'id':1})
    for enabled,ack in [(False,''),(True,''),(True,'incorrect')]:
        c=client(handler,enabled=enabled,ack=ack)
        with pytest.raises(GateWriteDisabled):
            await c.place_entry(contract='BTC_USDT',signed_contracts=1,client_id='t-test')
        await c.close()
    assert calls==[]


@pytest.mark.asyncio
async def test_exact_testnet_signatures_entry_stop_close_and_lookup():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host=='api-testnet.gateapi.io'
        digest=hashlib.sha512(request.content).hexdigest()
        signed=f'{request.method}\n{request.url.path}\n\n{digest}\n1000'
        assert request.headers['SIGN']==hmac.new(b'dummy-secret',signed.encode(),hashlib.sha512).hexdigest()
        return httpx.Response(200 if request.method=='GET' else 201,json={'id':123})
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    assert await c.place_entry(contract='BTC_USDT',signed_contracts=2,client_id='t-test')=={'id':'123'}
    assert await c.place_stop(contract='BTC_USDT',signed_position=2,reference_price='80000',stop_price='79000',tick_size='0.1')=={'id':'123'}
    await c.close_position(contract='BTC_USDT')
    await c.get_stop('123')
    await c.cancel_stop('123')
    assert json.loads(calls[0].content)['size']==2
    stop=json.loads(calls[1].content)
    assert stop['initial']['size']==0 and stop['initial']['close'] is True and stop['initial']['reduce_only'] is True
    assert stop['order_type']=='close-long-position' and stop['trigger']['rule']==2
    exit_body=json.loads(calls[2].content)
    assert exit_body['size']==0 and exit_body['close'] is True and exit_body['reduce_only'] is True
    assert [r.method for r in calls]==['POST','POST','POST','GET','DELETE']
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['timeout','server','redirect','bad-json','missing-id'])
async def test_ambiguous_write_is_never_retried_or_leaks_credentials(failure):
    calls=[]
    def handler(request):
        calls.append(request)
        if failure=='timeout':
            raise httpx.ReadTimeout('dummy-secret',request=request)
        if failure=='server': return httpx.Response(500,text='dummy-secret')
        if failure=='redirect': return httpx.Response(302,headers={'location':'https://other.example'})
        if failure=='bad-json': return httpx.Response(201,text='not json')
        return httpx.Response(201,json={'status':'ok'})
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    with pytest.raises(GateWriteUnknown) as error:
        await c.place_entry(contract='BTC_USDT',signed_contracts=1,client_id='t-test')
    assert 'dummy-secret' not in str(error.value) and len(calls)==1
    await c.close()


@pytest.mark.asyncio
async def test_invalid_ids_and_contracts_never_reach_network():
    calls=[]
    c=client(lambda req:(calls.append(req),httpx.Response(200,json={}))[1],enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    with pytest.raises(ValueError): await c.get_stop('../1')
    with pytest.raises(ValueError): await c.close_position(contract='DOGE_USDT')
    with pytest.raises(ValueError): await c._request('POST','/api/v4/withdrawals',body={})
    assert calls==[]
    await c.close()


@pytest.mark.asyncio
async def test_read_failure_is_sanitized():
    c=client(lambda req:httpx.Response(403,text='dummy-secret'))
    with pytest.raises(GateReadError) as error: await c.get_stop('123')
    assert 'dummy-secret' not in str(error.value)
    await c.close()

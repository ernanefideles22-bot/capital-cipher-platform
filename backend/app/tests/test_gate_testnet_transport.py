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
    assert stop['trigger']['expiration']==86400  # exact authenticated HTTP payload
    assert stop['pos_margin_mode']=='isolated' and stop['initial']['text']=='api'
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
async def test_position_lookup_is_testnet_read_only():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET'
        assert request.url.host=='api-testnet.gateapi.io'
        assert request.url.path=='/api/v4/futures/usdt/positions/BTC_USDT'
        return httpx.Response(200,json={'contract':'BTC_USDT','size':'0'})
    c=client(handler)
    assert (await c.get_position('BTC_USDT'))['size']=='0'
    with pytest.raises(ValueError): await c.get_position('OTHER')
    assert len(calls)==1
    await c.close()


@pytest.mark.asyncio
async def test_close_can_carry_bounded_client_identity_for_reconciliation():
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(201,json={'id':123})
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    await c.close_position(contract='BTC_USDT',client_id='t-exit-1')
    assert json.loads(calls[0].content)['text']=='t-exit-1'
    with pytest.raises(ValueError):
        await c.close_position(contract='BTC_USDT',client_id='../../unsafe')
    assert len(calls)==1
    await c.close()


@pytest.mark.asyncio
async def test_read_failure_is_sanitized():
    c=client(lambda req:httpx.Response(403,text='dummy-secret'))
    with pytest.raises(GateReadError) as error: await c.get_stop('123')
    assert 'dummy-secret' not in str(error.value)
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('label,expected', [
    ('POSITION_NOT_FOUND','POSITION_NOT_FOUND'),
    ('INVALID_PARAM_VALUE','INVALID_PARAM_VALUE'),
    ('NEW_PROVIDER_PARAMETER_CODE','NEW_PROVIDER_PARAMETER_CODE'),
    ('AUTO_INVALID_PARAM_TRIGGER_EXPIRATION','AUTO_INVALID_PARAM_TRIGGER_EXPIRATION'),
    ('BAD\nCODE',None),('X'*65,None),
    ('dummy-secret',None),(['INVALID_PARAM_VALUE'],None),
])
async def test_stop_failure_keeps_only_public_diagnostics_without_retry(label,expected):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(400,json={'label':label,'message':'dummy-key dummy-secret'})
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    with pytest.raises(GateWriteUnknown) as caught:
        await c.place_stop(contract='BTC_USDT',signed_position=3,
                           reference_price='77340',stop_price='76995.6',tick_size='0.1')
    assert caught.value.http_status==400 and caught.value.provider_label==expected
    assert caught.value.response_kind=='json_object'
    assert len(calls)==1
    assert 'dummy-secret' not in str(caught.value) and 'dummy-key' not in str(caught.value)
    assert 'dummy-secret' not in repr(vars(caught.value))
    await c.close()


@pytest.mark.asyncio
async def test_gate_actual_expiration_rejection_is_bounded_and_never_retried():
    calls=[]
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)['trigger']['expiration']==86400
        return httpx.Response(400,json={
            'label':'AUTO_INVALID_PARAM_TRIGGER_EXPIRATION',
            'message':'Invalid trigger.expiration parameter'
        })
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    with pytest.raises(GateWriteUnknown) as caught:
        await c.place_stop(contract='BTC_USDT',signed_position=3,
                           reference_price='77340',stop_price='76995.6',tick_size='0.1')
    assert caught.value.http_status==400
    assert caught.value.provider_label=='AUTO_INVALID_PARAM_TRIGGER_EXPIRATION'
    assert caught.value.provider_fields==('trigger','expiration')
    assert caught.value.response_kind=='json_object'
    assert len(calls)==1  # do not retry a potentially ambiguous exchange write
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body,kind,fields', [
    ({'label':'NEW_STOP_CODE','message':'Invalid initial.text value: dummy-secret dummy-key',
      'detail':'trigger.price must < last_price'},'json_object',('initial','trigger','text','price','last_price')),
    ({'label':42,'message':['dummy-secret']},'json_object',()),
    (['dummy-secret'],'json_other',()),
    ('dummy-secret <html>','invalid_json',()),
    ('x'*2_000_001,'oversized',()),
])
async def test_error_shape_and_schema_hints_never_retain_response_prose(body,kind,fields):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(400,**({'text':body} if isinstance(body,str) else {'json':body}))
    c=client(handler,enabled=True,ack='GATE_TESTNET_VIRTUAL_FUNDS_ONLY')
    with pytest.raises(GateWriteUnknown) as caught:
        await c.place_stop(contract='BTC_USDT',signed_position=3,
                           reference_price='77340',stop_price='76995.6',tick_size='0.1')
    assert caught.value.response_kind==kind and caught.value.provider_fields==fields
    assert len(calls)==1 and caught.value.__cause__ is None
    assert 'dummy-secret' not in repr(vars(caught.value))
    assert 'dummy-key' not in repr(vars(caught.value))
    assert 'must <' not in repr(vars(caught.value))
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('label',['UPPERCASE_SECRET','PREFIX_UPPERCASE_SECRET_SUFFIX','UPPERCASE'])
async def test_enum_shaped_credentials_are_redacted_too(label):
    c=GateTestnetTransport(Credentials('UPPERCASE_KEY','UPPERCASE_SECRET'),
        trusted_clock=lambda:1000,writes_enabled=True,acknowledgement='GATE_TESTNET_VIRTUAL_FUNDS_ONLY',
        transport=httpx.MockTransport(lambda req:httpx.Response(400,json={'label':label})))
    with pytest.raises(GateWriteUnknown) as caught:
        await c.place_entry(contract='BTC_USDT',signed_contracts=1,client_id='t-test')
    assert caught.value.provider_label is None
    await c.close()



@pytest.mark.asyncio
async def test_inventory_query_is_signed_and_pages_are_not_silently_truncated():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host=='api-testnet.gateapi.io'
        query=request.url.query.decode()
        digest=hashlib.sha512(request.content).hexdigest()
        signed=f'GET\n{request.url.path}\n{query}\n{digest}\n1000'
        assert request.headers['SIGN']==hmac.new(b'dummy-secret',signed.encode(),hashlib.sha512).hexdigest()
        assert request.url.params['contract']=='BTC_USDT'
        offset=int(request.url.params['offset'])
        return httpx.Response(200,json=[{'id':i} for i in range(1+offset,min(102,101+offset))])
    c=client(handler)
    rows=await c.rows('fills',contract='BTC_USDT')
    assert len(rows)==101 and len(calls)==2
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['duplicates','saturation','malformed','read_error'])
async def test_incomplete_inventory_never_becomes_empty_account(kind):
    calls=[]
    def handler(request):
        calls.append(request)
        offset=int(request.url.params['offset'])
        if kind=='read_error': return httpx.Response(503,text='dummy-secret')
        if kind=='malformed': return httpx.Response(200,json=[None])
        if kind=='duplicates': offset=0
        return httpx.Response(200,json=[{'id':i} for i in range(offset+1,offset+101)])
    c=client(handler)
    with pytest.raises(GateReadError): await c.rows('orders',status='open')
    assert len(calls)==(20 if kind=='saturation' else 2 if kind=='duplicates' else 1)
    await c.close()

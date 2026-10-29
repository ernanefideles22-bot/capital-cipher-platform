import hashlib
import hmac
import httpx
import pytest
from app.demo.gate_stop_probe import confirm_stop, read_verified_stop
from app.demo.gate_cycle_journal import GateCycleJournal


def row():
    return {'id_string':'123','status':'open','order_type':'close-long-position','create_time':1000,
            'initial':{'contract':'BTC_USDT','size':0,'price':'0','tif':'ioc','is_reduce_only':True,'is_close':True},
            'trigger':{'strategy_type':0,'price_type':1,'price':'79000','rule':2,'expiration':3600}}


async def no_sleep(_):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('use_time,expected', [(1011, True), (1020, False)])
async def test_evidence_is_revalidated_by_journal_before_protection(tmp_path, use_time, expected):
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json=row()))
    evidence=await read_verified_stop('key','secret',order_id='123',contract='BTC_USDT',
        position_size=2,trigger_price='79000',transport=transport,clock=lambda:1010,sleep=no_sleep)
    assert evidence is not None
    snapshot,observed=evidence
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    journal.create('trade_12345','BTC_USDT','79000',2)
    journal.record_entry_intent('trade_12345')
    journal.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    journal.record_stop_intent('trade_12345')
    journal.record_stop_acknowledgement('trade_12345',stop_id='123')
    assert journal.record_stop_confirmation('trade_12345',snapshot=snapshot,observed_at=observed,
           now=use_time,confirmed_position=2) is expected
    assert journal.read('trade_12345')['state']==('PROTECTED' if expected else 'HALTED')
    journal.close()


@pytest.mark.asyncio
async def test_no_evidence_returned_for_mismatched_stop():
    wrong=row()
    wrong['initial']['contract']='ETH_USDT'
    evidence=await read_verified_stop('key','secret',order_id='123',contract='BTC_USDT',
        position_size=2,trigger_price='79000',transport=httpx.MockTransport(lambda req:httpx.Response(200,json=wrong)),
        clock=lambda:1010,sleep=no_sleep)
    assert evidence is None


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


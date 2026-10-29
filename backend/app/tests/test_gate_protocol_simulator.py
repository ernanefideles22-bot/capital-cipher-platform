import hashlib
import hmac
import json
import httpx
import pytest

from app.demo.gate_protocol_simulator import GateProtocolSimulator, ExchangeOutcomeUnknown, SimulatedProtocolError
from app.demo.gate_order_plan import build_order_plan
from app.demo.gate_cycle_journal import GateCycleJournal, CycleBlocked


def plan():
    return build_order_plan(contract='BTC_USDT',signed_contracts=2,mark_price='80000',
                            stop_price='79000',tick_size='0.1',client_id='t-cycle-1')


@pytest.mark.asyncio
async def test_complete_simulated_cycle_signs_exact_body_and_reconciles(tmp_path):
    requests=[]
    proposal=plan()
    def handler(request):
        requests.append(request)
        assert request.url.host=='api-testnet.gateapi.io'
        digest=hashlib.sha512(request.content).hexdigest()
        msg=f'{request.method}\n{request.url.path}\n\n{digest}\n1000'
        assert request.headers['SIGN']==hmac.new(b'test-secret',msg.encode(),hashlib.sha512).hexdigest()
        if request.method=='POST':
            body=json.loads(request.content)
            if request.url.path.endswith('/price_orders'):
                assert body==proposal['stop']
                return httpx.Response(201,json={'id':123})
            if body.get('reduce_only'):
                assert body['size']==0 and body['close'] is True
                return httpx.Response(201,json={'id':125})
            assert body==proposal['entry']
            return httpx.Response(201,json={'id':124})
        initial=dict(proposal['stop']['initial'])
        initial['is_reduce_only']=initial.pop('reduce_only')
        initial['is_close']=initial.pop('close')
        return httpx.Response(200,json={'id_string':'123','status':'open','create_time':1000,
          'order_type':proposal['stop']['order_type'],'initial':initial,'trigger':proposal['stop']['trigger']})
    sim=GateProtocolSimulator(httpx.MockTransport(handler),clock=lambda:1000)
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    journal.create('trade_12345','BTC_USDT','79000',2)
    journal.record_entry_intent('trade_12345')
    entry=await sim.submit_entry(proposal)
    assert entry['id']==124
    # In production these facts must come from independent exchange reads.
    journal.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    journal.record_stop_intent('trade_12345')
    ack=await sim.submit_stop(proposal)
    journal.record_stop_acknowledgement('trade_12345',stop_id=str(ack['id']))
    observed=await sim.get_stop('123')
    assert journal.record_stop_confirmation('trade_12345',snapshot=observed,observed_at=1010,now=1010,confirmed_position=2)
    journal.record_exit_intent('trade_12345')
    await sim.submit_exit(proposal)
    assert journal.record_flat_confirmation('trade_12345',position=0,open_orders=0,
           open_stop_orders=0,all_positions_flat=True,exchange_confirmed=True)
    assert journal.read('trade_12345')['state']=='CLOSED' and len(requests)==4
    journal.close()


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_persisted_intent_blocks_resubmission(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic timeout',request=request)
    simulator=GateProtocolSimulator(httpx.MockTransport(handler),clock=lambda:1000)
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    journal.create('trade_12345','BTC_USDT','79000',2)
    journal.record_entry_intent('trade_12345')
    with pytest.raises(ExchangeOutcomeUnknown):
        await simulator.submit_entry(plan())
    assert len(calls)==1
    assert journal.read('trade_12345')['state']=='ENTRY_PENDING'
    with pytest.raises(CycleBlocked):
        journal.record_entry_intent('trade_12345')
    journal.halt_unknown('trade_12345')
    journal.close()


@pytest.mark.asyncio
async def test_stop_ack_is_not_confirmation_and_bad_snapshot_halts(tmp_path):
    simulator=GateProtocolSimulator(httpx.MockTransport(lambda req:httpx.Response(201,json={'id':123})),clock=lambda:1000)
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    journal.create('trade_12345','BTC_USDT','79000',2)
    journal.record_entry_intent('trade_12345')
    journal.record_entry_confirmation('trade_12345',signed_position=2,owned_order_confirmed=True)
    journal.record_stop_intent('trade_12345')
    ack=await simulator.submit_stop(plan())
    journal.record_stop_acknowledgement('trade_12345',stop_id=str(ack['id']))
    assert journal.read('trade_12345')['state']=='STOP_PENDING'
    assert not journal.record_stop_confirmation('trade_12345',snapshot={'id':123},observed_at=1010,now=1010,confirmed_position=2)
    assert journal.read('trade_12345')['state']=='HALTED'
    journal.close()


def test_no_live_transport_or_user_credentials_accepted():
    with pytest.raises(SimulatedProtocolError):
        GateProtocolSimulator(httpx.AsyncHTTPTransport(),clock=lambda:1000)
    with pytest.raises(TypeError):
        GateProtocolSimulator(httpx.MockTransport(lambda req:None),clock=lambda:1000,secret='real')

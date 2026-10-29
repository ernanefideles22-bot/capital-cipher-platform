"""Integration test with in-memory exchange responses only."""
import httpx
import pytest
from app.demo.gate_cycle_journal import GateCycleJournal
from app.demo.gate_risk_check import assess
from app.demo.gate_testnet_transport import GateTestnetTransport, TestnetCredentials as Credentials


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [False, True])
async def test_preflight_bound_entry_stop_and_close(tmp_path, stale):
    calls=[]
    stop_snapshot={'id_string':'456','status':'open','create_time':1000,'order_type':'close-long-position',
        'initial':{'contract':'BTC_USDT','size':0,'price':'0','tif':'ioc','is_reduce_only':True,'is_close':True},
        'trigger':{'strategy_type':0,'price_type':1,'price':'79000','rule':2,'expiration':3600}}
    def handler(req):
        calls.append((req.method,req.url.path))
        assert req.url.host=='api-testnet.gateapi.io'
        if req.method=='POST' and req.url.path.endswith('/price_orders'):
            return httpx.Response(201,json={'id':'456'})
        if req.method=='POST': return httpx.Response(201,json={'id':'123'})
        if req.url.path.endswith('/orders/123'):
            return httpx.Response(200,json={'id':'123','contract':'BTC_USDT','text':'t-cycle-1','size':'1',
                'left':'0','status':'finished','finish_as':'filled','is_close':False,'is_reduce_only':False})
        if req.url.path.endswith('/positions/BTC_USDT'):
            return httpx.Response(200,json={'contract':'BTC_USDT','mode':'single','size':'1'})
        if req.url.path.endswith('/price_orders/456'): return httpx.Response(200,json=stop_snapshot)
        return httpx.Response(404)
    client=GateTestnetTransport(Credentials('dummy-key','dummy-secret'),trusted_clock=lambda:1000,
        writes_enabled=True,acknowledgement='GATE_TESTNET_VIRTUAL_FUNDS_ONLY',transport=httpx.MockTransport(handler))
    j=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    check=assess(contract='BTC_USDT',side='LONG',quantity=1,multiplier='0.0001',price='80000',
        stop='79000',equity='1000',available='1000',now=1000,
        config={'mode':'single','margin':'isolated','leverage':'1','observed_at':995},
        inventory={'positions':0,'orders':0,'conditional_orders':0,'observed_at':995},
        central={'kill_switch_active':False,'durable_control_active':False,'observed_at':995},
        approval={'approved':True,'status':'APPROVED','id':'abc','contract':'BTC_USDT','side':'LONG',
                  'observed_at':995,'max_notional':'10','max_loss':'1'})
    assert check['preflight_passed'] and check['execution_authorized'] is False
    j.create('trade_12345','BTC_USDT','79000',1)
    j.record_bound_entry_intent('trade_12345','t-cycle-1')
    ack=await client.place_entry(contract='BTC_USDT',signed_contracts=1,client_id='t-cycle-1')
    j.record_entry_acknowledgement('trade_12345',ack['id'])
    order=await client.get_order(ack['id'])
    position=await client.get_position('BTC_USDT')
    verified=j.record_verified_entry('trade_12345',order=order,position=position,
        order_observed_at=990 if stale else 1000,position_observed_at=1000,now=1000)
    if stale:
        assert not verified and j.read('trade_12345')['state']=='HALTED'
        assert not any(path.endswith('/price_orders') for _,path in calls)
    else:
        assert verified
        j.record_stop_intent('trade_12345')
        ack=await client.place_stop(contract='BTC_USDT',signed_position=1,reference_price='80000',stop_price='79000',tick_size='0.1')
        j.record_stop_acknowledgement('trade_12345',stop_id=ack['id'])
        stop=await client.get_stop(ack['id'])
        assert j.record_stop_confirmation('trade_12345',snapshot=stop,observed_at=1000,now=1000,confirmed_position=1)
        assert j.read('trade_12345')['state']=='PROTECTED'
        j.record_exit_intent('trade_12345')
        await client.close_position(contract='BTC_USDT')
        # Fixture evidence only: a real executor must perform independent reads.
        assert j.record_flat_confirmation('trade_12345',position=0,open_orders=0,
            open_stop_orders=0,all_positions_flat=True,exchange_confirmed=True)
    j.close()
    await client.close()

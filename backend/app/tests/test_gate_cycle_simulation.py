import httpx
import pytest
from app.demo.gate_cycle_simulation import run_simulated_cycle, SimulationBlocked
from app.demo.gate_cycle_journal import GateCycleJournal, CycleBlocked
from app.demo.gate_order_plan import build_order_plan
from app.demo.gate_protocol_simulator import GateProtocolSimulator


def plan():
    return build_order_plan(contract='BTC_USDT',signed_contracts=2,mark_price='80000',
        stop_price='79000',tick_size='0.1',client_id='t-sim-cycle')


def snapshot():
    return {'id_string':'123','status':'open','create_time':1000,'order_type':'close-long-position',
        'initial':{'contract':'BTC_USDT','size':0,'price':'0','tif':'ioc','is_reduce_only':True,'is_close':True},
        'trigger':{'strategy_type':0,'price_type':1,'price':'79000','rule':2,'expiration':3600}}


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,expected,calls', [
    ('none','CLOSED',3), ('entry_timeout','HALTED',1), ('unowned','HALTED',1),
    ('stop_timeout','HALTED',2), ('stop_stale','HALTED',2), ('wrong_stop','HALTED',2),
    ('exit_timeout','HALTED',3), ('nonflat','HALTED',3),
])
async def test_cycle_and_faults_are_fail_closed(tmp_path, fault, expected, calls):
    requests=[]
    def handler(request):
        requests.append(request)
        number=len(requests)
        if fault in {'entry_timeout','stop_timeout','exit_timeout'} and number=={'entry_timeout':1,'stop_timeout':2,'exit_timeout':3}[fault]:
            raise httpx.ReadTimeout('simulated timeout', request=request)
        return httpx.Response(201,json={'id':123})
    sim=GateProtocolSimulator(httpx.MockTransport(handler),clock=lambda:1000)
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    async def entry(_):
        return {'position':2,'owned':fault!='unowned'}
    async def stop(_):
        row=snapshot()
        if fault=='wrong_stop': row['initial']['contract']='ETH_USDT'
        return {'snapshot':row,'observed_at':1000,'now':1010 if fault=='stop_stale' else 1001,'position':2}
    async def exit(_):
        return {'position':1 if fault=='nonflat' else 0,'open_orders':0,'open_stop_orders':0,
                'all_positions_flat':fault!='nonflat','exchange_confirmed':True}
    result=await run_simulated_cycle(simulator=sim,journal=journal,cycle_id='trade_12345',
        plan=plan(),read_entry=entry,read_stop=stop,read_exit=exit)
    assert result==expected and journal.read('trade_12345')['state']==expected
    assert len(requests)==calls
    if expected=='HALTED':
        with pytest.raises(CycleBlocked): journal.create('other_12345','BTC_USDT','79000',1)
    journal.close()


@pytest.mark.asyncio
async def test_real_adapter_rejected_before_any_action(tmp_path):
    journal=GateCycleJournal(str(tmp_path/'cycle.sqlite'))
    with pytest.raises(SimulationBlocked):
        await run_simulated_cycle(simulator=object(),journal=journal,cycle_id='trade_12345',
                                  plan=plan(),read_entry=None,read_stop=None,read_exit=None)
    assert journal.db.execute('SELECT COUNT(*) FROM cycles').fetchone()[0]==0
    journal.close()

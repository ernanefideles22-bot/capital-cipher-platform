import pytest
from app.demo.gate_risk_check import assess, EntryBlocked


def inputs():
    return dict(contract='BTC_USDT',side='LONG',quantity=1,multiplier='0.0001',price='80000',stop='79000',
        equity='1000',available='1000',now=1000,
        config={'mode':'single','margin':'isolated','leverage':'1','observed_at':995},
        inventory={'positions':0,'orders':0,'conditional_orders':0,'observed_at':995},
        central={'kill_switch_active':False,'durable_control_active':False,'observed_at':995},
        approval={'approved':True,'status':'APPROVED','id':'abc','contract':'BTC_USDT','side':'LONG',
                  'observed_at':995,'max_notional':'10','max_loss':'1'})


def test_preflight_pass_does_not_authorize_execution():
    result=assess(**inputs())
    assert result['preflight_passed'] and result['execution_authorized'] is False
    assert result['notional_usdt']=='8.0000'


@pytest.mark.parametrize('section,field,value',[
    ('config','margin','cross'),('config','leverage','76'),('config','mode','dual_long'),
    ('config','observed_at',980),('inventory','positions',1),('inventory','orders',1),
    ('inventory','conditional_orders',1),('inventory','observed_at',980),
    ('central','kill_switch_active',True),('central','durable_control_active',True),
    ('approval','approved',False),('approval','contract','ETH_USDT'),('approval','observed_at',960),
    ('approval','max_loss','0'),
])
def test_missing_risk_safety_or_stale_evidence_blocks_entry(section,field,value):
    kwargs=inputs();kwargs[section][field]=value
    with pytest.raises(EntryBlocked): assess(**kwargs)


@pytest.mark.parametrize('override',[
    {'quantity':0}, {'quantity':1.1}, {'quantity':100}, {'stop':'81000'},
    {'equity':'NaN'}, {'available':0}, {'side':'SHORT'}, {'contract':'DOGE_USDT'},
])
def test_invalid_or_over_budget_order_is_blocked(override):
    kwargs=inputs();kwargs.update(override)
    with pytest.raises(EntryBlocked): assess(**kwargs)

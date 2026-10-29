import copy
import pytest
from app.demo.gate_entry_evidence import confirmed_entry


def evidence():
    return {'order':{'id':'123','contract':'BTC_USDT','text':'t-cycle-1',
                     'size':'2','left':'0','status':'finished','finish_as':'filled',
                     'is_close':False,'is_reduce_only':False},
            'position':{'contract':'BTC_USDT','mode':'single','size':'2'},
            'contract':'BTC_USDT','order_id':'123','client_id':'t-cycle-1','planned_size':2,
            'order_observed_at':1000,'position_observed_at':1001,'now':1002}


def test_full_and_partial_fills_agree_with_independent_position():
    data=evidence()
    assert confirmed_entry(**data)==2
    data['order'].update(left='1',finish_as='ioc')
    data['position']['size']='1'
    assert confirmed_entry(**data)==1
    data['planned_size']=-2
    data['order']['size']='-2'
    data['position']['size']='-1'
    assert confirmed_entry(**data)==-1


@pytest.mark.parametrize('target,key,value',[
    ('order','id','124'),('order','contract','ETH_USDT'),('order','text','t-other'),
    ('order','status','open'),('order','finish_as','cancelled'),('order','is_close',True),
    ('order','is_reduce_only',True),('order','size','3'),('order','left','3'),
    ('order','left','2'),('order','left','NaN'),('position','contract','ETH_USDT'),
    ('position','mode','dual_long'),('position','size','1'),
    (None,'order_observed_at',990),(None,'position_observed_at',1010),
    (None,'planned_size',True),(None,'order_id','../123'),
])
def test_never_confirms_mismatched_stale_or_missing_evidence(target,key,value):
    data=evidence()
    (data[target] if target else data)[key]=value
    assert confirmed_entry(**data) is None


def test_missing_fields_do_not_confirm():
    data=evidence()
    del data['order']['left']
    assert confirmed_entry(**data) is None

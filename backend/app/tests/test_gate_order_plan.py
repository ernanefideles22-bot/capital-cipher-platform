import pytest
from app.demo.gate_order_plan import build_order_plan, InvalidGatePlan
from app.demo.gate_stop_validation import stop_matches


def plan(**updates):
    args={'contract':'BTC_USDT','signed_contracts':2,'mark_price':'80000','stop_price':'79000',
          'tick_size':'0.1','client_id':'t-test-123'}
    args.update(updates)
    return build_order_plan(**args)


def test_long_entry_protective_stop_and_reduce_only_exit():
    p=plan()
    assert p['execution_enabled'] is False and p['environment']=='GATE_TESTNET'
    assert p['entry']['size']==2 and p['entry']['price']=='0' and p['entry']['tif']=='ioc'
    assert p['stop']['initial']['size']==0 and p['stop']['initial']['close'] is True and p['stop']['initial']['reduce_only'] is True
    assert p['stop']['trigger']['rule']==2 and p['stop']['trigger']['price_type']==1
    assert p['stop']['trigger']['expiration']==86400  # finite documented Gate API example
    assert p['stop']['order_type']=='close-long-position'
    assert p['stop']['pos_margin_mode']=='isolated' and p['stop']['initial']['text']=='api'
    assert p['emergency_exit']['size']==0 and p['emergency_exit']['close'] is True and p['emergency_exit']['reduce_only'] is True


def test_short_reverses_direction_and_stop_rule():
    p=plan(signed_contracts=-3,stop_price='81000')
    assert p['entry']['size']==-3 and p['stop']['initial']['size']==0 and p['stop']['initial']['close'] is True
    assert p['stop']['trigger']['rule']==1 and p['stop']['order_type']=='close-short-position'
    assert p['stop']['trigger']['expiration']==86400
    assert p['stop']['pos_margin_mode']=='isolated' and p['stop']['initial']['text']=='api'


@pytest.mark.parametrize('qty,stop', [(2,'79000'),(-3,'81000')])
def test_simulated_exchange_confirmation_must_match_executed_position(qty,stop):
    proposal=plan(signed_contracts=qty,stop_price=stop)
    initial=dict(proposal['stop']['initial'])
    initial['is_reduce_only']=initial.pop('reduce_only')
    initial['is_close']=initial.pop('close')
    snapshot={'id_string':'345','status':'open','create_time':1000,
              'order_type':proposal['stop']['order_type'],'initial':initial,
              'trigger':proposal['stop']['trigger']}
    check=dict(order_id='345',contract='BTC_USDT',position_size=qty,
               trigger_price=stop,observed_at=1010,now=1010)
    assert stop_matches(snapshot,**check)
    check['position_size']=-qty
    assert not stop_matches(snapshot,**check)


@pytest.mark.parametrize('override', [
    {'contract':'DOGE_USDT'}, {'signed_contracts':0}, {'signed_contracts':1.5},
    {'signed_contracts':True}, {'mark_price':'NaN'}, {'stop_price':'80100'},
    {'stop_price':'79000.05'}, {'tick_size':0}, {'client_id':'other'},
    {'client_id':'t-unsafe spaces'}, {'expiration_seconds':0},
    {'expiration_seconds':3600}, {'expiration_seconds':86401},
    {'expiration_seconds':False},
])
def test_invalid_or_unprotected_plans_fail_closed(override):
    with pytest.raises(InvalidGatePlan):
        plan(**override)

import pytest
from app.demo.gate_exit_evidence import confirmed_exit


def sample():
    return {"order":{"id":"123","text":"t-exit-1","contract":"BTC_USDT","status":"finished",
                     "finish_as":"filled","size":"0","left":"0","is_close":True,
                     "is_reduce_only":True,"is_liq":False},
            "position":{"contract":"BTC_USDT","mode":"single","size":"0"},
            "inventory":{"positions":0,"orders":0,"conditional_orders":0,"atomic":False,
                         "execution_authorized":False},
            "contract":"BTC_USDT","order_id":"123","client_id":"t-exit-1",
            "order_observed_at":1000,"position_observed_at":1001,"inventory_started_at":1000,
            "inventory_finished_at":1002,"now":1003}


def test_completed_close_and_empty_non_atomic_inventory():
    assert confirmed_exit(**sample())


@pytest.mark.parametrize("section,key,value", [
    ("order","id","124"),("order","text","t-other"),("order","contract","ETH_USDT"),
    ("order","status","open"),("order","finish_as","ioc"),("order","size","1"),
    ("order","left","1"),("order","is_close",False),("order","is_reduce_only",False),
    ("order","is_liq",True),("position","size","1"),("position","mode","dual_long"),
    ("inventory","positions",1),("inventory","orders",1),("inventory","conditional_orders",1),
    ("inventory","atomic",True),("inventory","execution_authorized",True),
    (None,"order_observed_at",990),(None,"inventory_started_at",990),
    (None,"inventory_finished_at",1010),(None,"client_id","../escape"),
])
def test_uncertain_or_mismatched_close_is_never_confirmed(section,key,value):
    data=sample()
    (data[section] if section else data)[key]=value
    assert not confirmed_exit(**data)


def test_missing_close_evidence_fails_closed():
    data=sample()
    del data["order"]["is_liq"]
    assert not confirmed_exit(**data)


@pytest.mark.parametrize('opened', [3, -3])
def test_normalized_signed_execution_requires_exact_persisted_position(opened):
    data=sample()
    data['order']['size']=str(-opened)
    assert not confirmed_exit(**data)  # No caller-supplied exposure proof.
    assert confirmed_exit(**data,expected_signed_position=opened)


@pytest.mark.parametrize('expected', [0, True, 'NaN', 'Infinity', '2.5', 2, -3])
def test_normalized_close_rejects_invalid_or_different_exposure(expected):
    data=sample()
    data['order']['size']='-3'
    assert not confirmed_exit(**data,expected_signed_position=expected)


@pytest.mark.parametrize('section,key,value', [
    ('order','text','t-unowned'),('order','id','999'),('order','is_close',False),
    ('order','is_reduce_only',False),('order','is_liq',True),('order','left',1),
    ('order','contract','ETH_USDT'),('position','size',1),
    ('inventory','orders',1),('inventory','conditional_orders',1),
    ('inventory','positions',1),
])
def test_normalized_close_retains_identity_flatness_and_inventory_guards(section,key,value):
    data=sample()
    data['order']['size']='-3'
    data[section][key]=value
    assert not confirmed_exit(**data,expected_signed_position=3)

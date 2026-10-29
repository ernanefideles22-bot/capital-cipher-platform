import pytest
from app.demo.gate_stop_validation import stop_matches


def stop():
    return {'id_string':'123', 'status':'open', 'order_type':'plan-close-long-position', 'create_time':1000,
            'initial': {'contract':'BTC_USDT', 'size':-3,
            'price':'0', 'tif':'ioc', 'is_reduce_only':True},
            'trigger': {'strategy_type':0, 'price_type':1, 'price':'79000', 'rule':2, 'expiration':3600}}


def valid(row):
    return stop_matches(row, order_id='123', contract='BTC_USDT', position_size=3, trigger_price='79000', observed_at=1010, now=1010)


def test_active_matching_stop():
    assert valid(stop())


@pytest.mark.parametrize('section,key,value', [
    (None,'status','finished'), (None,'id_string','124'), (None,'order_type','plan-close-short-position'), ('initial','contract','ETH_USDT'),
    ('initial','size',-2), ('initial','is_reduce_only',False), ('initial','price','79000'),
    ('initial','tif','gtc'), ('trigger','rule',1), ('trigger','price_type',0),
    ('trigger','price','78000'), ('trigger','expiration',0),
])
def test_rejects_unconfirmed_or_mismatched_stop(section,key,value):
    row=stop()
    (row if section is None else row[section])[key]=value
    assert not valid(row)


def test_short_position_and_decimal_amount_precedence():
    row=stop()
    row['initial'].update(size=0, amount='3')
    row['order_type']='plan-close-short-position'
    row['trigger']['rule']=1
    assert stop_matches(row, order_id='123', contract='BTC_USDT', position_size=-3, trigger_price='79000', observed_at=1010, now=1010)
    row['initial']['amount']='2'
    assert not stop_matches(row, order_id='123', contract='BTC_USDT', position_size=-3, trigger_price='79000', observed_at=1010, now=1010)


def test_missing_or_nonfinite_values_are_rejected():
    row=stop(); del row['initial']['is_reduce_only']
    assert not valid(row)


def test_stale_snapshot_and_near_expiry_are_rejected():
    row=stop()
    assert not stop_matches(row, order_id='123', contract='BTC_USDT', position_size=3, trigger_price='79000', observed_at=1000, now=1010)
    assert not stop_matches(row, order_id='123', contract='BTC_USDT', position_size=3, trigger_price='79000', observed_at=4590, now=4590)
    row=stop(); row['trigger']['price']='NaN'
    assert not valid(row)

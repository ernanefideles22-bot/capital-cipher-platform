"""Conservative validation of a Gate futures price-triggered stop snapshot.

This is a local verification primitive only, not an order executor. A caller
must obtain a fresh exchange response for the specific accepted stop order.
"""
from decimal import Decimal, InvalidOperation


def stop_configuration_matches(snapshot, *, order_id, contract, position_size, trigger_price):
    try:
        if not isinstance(snapshot, dict) or isinstance(position_size, bool):
            return False
        position = Decimal(str(position_size))
        target = Decimal(str(trigger_price))
        initial, trigger = snapshot['initial'], snapshot['trigger']
        size = Decimal(str(initial['amount'] if 'amount' in initial else initial['size']))
        price = Decimal(str(trigger['price']))
        execution_price = Decimal(str(initial['price']))
        created = Decimal(str(snapshot['create_time']))
        response_id = snapshot.get('id_string', snapshot.get('id'))
        if any(not n.is_finite() for n in (position, target, size, price, execution_price, created)):
            return False
        if position == 0 or target <= 0 or not order_id or not contract:
            return False
        return bool(
            str(response_id) == str(order_id)
            and snapshot['order_type'] == ('close-long-position' if position > 0 else 'close-short-position')
            and initial['contract'] == contract
            and initial.get('is_reduce_only') is True
            and initial.get('is_close') is True
            and size == 0
            and type(trigger['strategy_type']) is int
            and type(trigger['price_type']) is int
            and type(trigger['rule']) is int
            and trigger['strategy_type'] == 0
            and trigger['price_type'] == 1
            and trigger['rule'] == (2 if position > 0 else 1)
            and price == target
            and execution_price == 0
            and initial['tif'] == 'ioc'
            and isinstance(trigger['expiration'], int)
            and not isinstance(trigger['expiration'], bool)
            and trigger['expiration'] > 0
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def stop_matches(snapshot, *, order_id, contract, position_size, trigger_price,
                 observed_at, now, min_remaining_seconds=60):
    if not stop_configuration_matches(snapshot,order_id=order_id,contract=contract,
            position_size=position_size,trigger_price=trigger_price):
        return False
    try:
        if any(isinstance(v,bool) for v in (observed_at,now)): return False
        observed,current,created=map(lambda v:Decimal(str(v)),(observed_at,now,snapshot['create_time']))
        return (all(v.is_finite() and v>0 for v in (observed,current,created))
            and type(min_remaining_seconds) is int and min_remaining_seconds>=0
            and snapshot['status']=='open' and 0<=current-observed<=5 and current>=created
            and created+snapshot['trigger']['expiration']-current>=min_remaining_seconds)
    except (KeyError,ValueError,TypeError,ArithmeticError):
        return False

"""Conservative validation of a Gate futures price-triggered stop snapshot.

This is a local verification primitive only, not an order executor. A caller
must obtain a fresh exchange response for the specific accepted stop order.
"""
from decimal import Decimal, InvalidOperation


def stop_matches(snapshot, *, order_id, contract, position_size, trigger_price):
    try:
        if not isinstance(snapshot, dict) or isinstance(position_size, bool):
            return False
        position = Decimal(str(position_size))
        target = Decimal(str(trigger_price))
        initial, trigger = snapshot['initial'], snapshot['trigger']
        size = Decimal(str(initial['amount'] if 'amount' in initial else initial['size']))
        price = Decimal(str(trigger['price']))
        execution_price = Decimal(str(initial['price']))
        response_id = snapshot.get('id_string', snapshot.get('id'))
        if any(not n.is_finite() for n in (position, target, size, price, execution_price)):
            return False
        if position == 0 or target <= 0 or not order_id or not contract:
            return False
        return bool(
            str(response_id) == str(order_id)
            and snapshot['status'] == 'open'
            and initial['contract'] == contract
            and initial.get('is_reduce_only') is True
            and size == -position
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

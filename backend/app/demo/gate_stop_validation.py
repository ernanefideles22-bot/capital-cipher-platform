"""Conservative validation of a Gate futures price-triggered stop snapshot.

This is a local verification primitive only, not an order executor. A caller
must obtain a fresh exchange response for the specific accepted stop order.
"""
from decimal import Decimal, InvalidOperation


def stop_matches(snapshot, *, order_id, contract, position_size, trigger_price,
                 observed_at, now, min_remaining_seconds=60):
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
        observed = Decimal(str(observed_at))
        current = Decimal(str(now))
        response_id = snapshot.get('id_string', snapshot.get('id'))
        if any(not n.is_finite() for n in (position, target, size, price, execution_price, created, observed, current)):
            return False
        if position == 0 or target <= 0 or not order_id or not contract or type(min_remaining_seconds) is not int or min_remaining_seconds < 0:
            return False
        return bool(
            str(response_id) == str(order_id)
            and snapshot['status'] == 'open'
            and snapshot['order_type'] == ('plan-close-long-position' if position > 0 else 'plan-close-short-position')
            and initial['contract'] == contract
            and initial.get('is_reduce_only') is True
            and size == -position
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
            and 0 <= current - observed <= 5
            and 0 <= current - created
            and created + trigger['expiration'] - current >= min_remaining_seconds
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False

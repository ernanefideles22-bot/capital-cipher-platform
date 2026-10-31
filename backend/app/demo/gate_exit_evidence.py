"""Read-only validation of a Gate Testnet close and account inventory."""
from decimal import Decimal, InvalidOperation
import re


def confirmed_close_order(*, order, position, contract, order_id, client_id,
                          order_observed_at, position_observed_at, now,
                          expected_signed_position=None):
    """Validate only an owned reduce-only close, before canceling its stop.

    This does not certify flat account inventory or release the journal slot.
    """
    try:
        if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'}:
            return False
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            return False
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
            return False
        raw=(order['size'],order['left'],position['size'],order_observed_at,position_observed_at,now)
        if any(isinstance(x,bool) or x is None for x in raw):
            return False
        size,left,pos,order_t,pos_t,current=(Decimal(str(x)) for x in raw)
        if any(not n.is_finite() for n in (size,left,pos,order_t,pos_t,current)):
            return False
        # Gate can return the executed signed quantity for a size=0 close.
        # Accept it only against independently persisted pre-close exposure.
        size_matches = size == 0
        if expected_signed_position is not None:
            if isinstance(expected_signed_position, bool):
                return False
            expected = Decimal(str(expected_signed_position))
            if not expected.is_finite() or expected == 0 or expected != expected.to_integral_value():
                return False
            size_matches = size == 0 or size == -expected
        return (min(order_t,pos_t,current) > 0
                and 0 <= current-order_t <= 5 and 0 <= current-pos_t <= 5
                and str(order.get('id_string') or order.get('id') or '') == order_id
                and order['text'] == client_id and order['contract'] == contract and position['contract'] == contract
                and order['status'] == 'finished' and order['finish_as'] == 'filled'
                and order.get('is_close') is True and order.get('is_reduce_only') is True
                and order.get('is_liq') is False and size_matches and left == 0 and pos == 0
                and position.get('mode') == 'single')
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError,AttributeError):
        return False


def confirmed_exit(*, order, position, inventory, contract, order_id, client_id,
                   order_observed_at, position_observed_at, inventory_started_at,
                   inventory_finished_at, now, expected_signed_position=None):
    """Confirm a bound close AND independently read flat account inventory.

    Separate reads are not atomic. The caller must hold exclusive account
    ownership. This validator never authorizes a new order.
    """
    if not confirmed_close_order(order=order,position=position,contract=contract,
            order_id=order_id,client_id=client_id,order_observed_at=order_observed_at,
            position_observed_at=position_observed_at,now=now,
            expected_signed_position=expected_signed_position):
        return False
    try:
        raw=(inventory_started_at,inventory_finished_at,now)
        if any(isinstance(x,bool) or x is None for x in raw): return False
        started,finished,current=(Decimal(str(x)) for x in raw)
        if any(not n.is_finite() for n in (started,finished,current)): return False
        return (min(started,finished,current)>0 and 0<=finished-started<=5 and 0<=current-finished<=5
            and inventory.get('execution_authorized') is False and inventory.get('atomic') is False
            and all(type(inventory.get(key)) is int and inventory[key]==0
                    for key in ('positions','orders','conditional_orders')))
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError,AttributeError):
        return False

"""Read-only validation of a Gate Testnet close and account inventory."""
from decimal import Decimal, InvalidOperation
import re


def confirmed_exit(*, order, position, inventory, contract, order_id, client_id,
                   order_observed_at, position_observed_at, inventory_started_at,
                   inventory_finished_at, now):
    """Confirm a close from separately acquired reads, not an atomic account snapshot.

    The caller must also ensure exclusive ownership of the account and secure
    independent exchange reads. A valid result never authorizes a new order.
    """
    try:
        if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'}:
            return False
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            return False
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
            return False
        if not all(isinstance(x,dict) for x in (order,position,inventory)):
            return False
        raw=(order['size'],order['left'],position['size'],order_observed_at,
             position_observed_at,inventory_started_at,inventory_finished_at,now)
        if any(isinstance(x,bool) or x is None for x in raw):
            return False
        size,left,pos,order_t,pos_t,started,finished,current=(Decimal(str(x)) for x in raw)
        if any(not n.is_finite() for n in (size,left,pos,order_t,pos_t,started,finished,current)):
            return False
        if min(order_t,pos_t,started,finished,current) <= 0:
            return False
        if not (0 <= current-order_t <= 5 and 0 <= current-pos_t <= 5
                and 0 <= finished-started <= 5 and 0 <= current-finished <= 5):
            return False
        if str(order.get('id_string') or order.get('id') or '') != order_id:
            return False
        if order['text'] != client_id or order['contract'] != contract or position['contract'] != contract:
            return False
        if order['status'] != 'finished' or order['finish_as'] != 'filled':
            return False
        if order.get('is_close') is not True or order.get('is_reduce_only') is not True or order.get('is_liq') is not False:
            return False
        if size != 0 or left != 0 or pos != 0 or position.get('mode') != 'single':
            return False
        if inventory.get('execution_authorized') is not False or inventory.get('atomic') is not False:
            return False
        return all(type(inventory.get(key)) is int and inventory[key] == 0
                   for key in ('positions','orders','conditional_orders'))
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError):
        return False

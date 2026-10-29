"""Validate independent Testnet order and position evidence; never sends orders."""
from decimal import Decimal, InvalidOperation
import re


def confirmed_entry(*, order, position, contract, order_id, client_id, planned_size,
                    order_observed_at, position_observed_at, now):
    """Return signed filled size or None. Separate reads are never atomic."""
    try:
        if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'}:
            return None
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
            return None
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            return None
        if not isinstance(order,dict) or not isinstance(position,dict):
            return None
        if any(isinstance(x,bool) for x in (planned_size,order_observed_at,position_observed_at,now)):
            return None
        planned, size, left, pos, order_time, pos_time, current = (
            Decimal(str(x)) for x in (planned_size,order['size'],order['left'],position['size'],
                                      order_observed_at,position_observed_at,now))
        if any(not n.is_finite() for n in (planned,size,left,pos,order_time,pos_time,current)):
            return None
        if not (0 <= current-order_time <= 5 and 0 <= current-pos_time <= 5):
            return None
        if order['contract'] != contract or position['contract'] != contract or order['text'] != client_id:
            return None
        if str(order.get('id_string') or order.get('id') or '') != order_id:
            return None
        if order['status'] != 'finished' or order['finish_as'] not in {'filled','ioc'}:
            return None
        if order.get('is_close') is not False or order.get('is_reduce_only') is not False:
            return None
        if position.get('mode') != 'single' or planned == 0 or size != planned:
            return None
        if size != size.to_integral_value() or left != left.to_integral_value() or abs(left) > abs(size):
            return None
        # The provider may express remaining quantity with either sign.
        filled = (abs(size)-abs(left)) * (1 if size > 0 else -1)
        if filled == 0 or pos != filled:
            return None
        if order['finish_as'] == 'filled' and left != 0:
            return None
        return int(filled)
    except (KeyError,TypeError,ValueError,InvalidOperation,OverflowError):
        return None

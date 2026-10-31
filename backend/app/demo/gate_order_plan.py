"""Build and validate a research-only Gate Testnet order plan.

This module serializes intent but does not transmit requests or authorize an
order. Rules and fill quantities must be revalidated by a future executor.
"""
from decimal import Decimal, InvalidOperation
import re


class InvalidGatePlan(ValueError):
    pass


def _number(value):
    if isinstance(value, bool):
        raise InvalidGatePlan('invalid number')
    try:
        result=Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidGatePlan('invalid number') from None
    if not result.is_finite():
        raise InvalidGatePlan('invalid number')
    return result


def build_order_plan(*, contract, signed_contracts, mark_price, stop_price,
                     tick_size, client_id, expiration_seconds=3600):
    """Build market entry, conditional stop and reduce-only exit payloads.

    Prices and quantities are not rounded. `signed_contracts` is the number of
    contracts, not BTC/ETH units. This plan requires one-way isolated mode.
    """
    if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'}:
        raise InvalidGatePlan('unsupported contract')
    if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}', client_id):
        raise InvalidGatePlan('invalid client id')
    qty, mark, stop, tick = map(_number, (signed_contracts, mark_price, stop_price, tick_size))
    if qty == 0 or qty != qty.to_integral_value() or abs(qty)>1000000:
        raise InvalidGatePlan('invalid contract quantity')
    if min(mark,stop,tick) <= 0 or stop % tick != 0:
        raise InvalidGatePlan('invalid price or tick')
    if not (stop < mark if qty > 0 else stop > mark):
        raise InvalidGatePlan('stop is not adverse to position')
    if type(expiration_seconds) is not int or not 300 <= expiration_seconds <= 86400:
        raise InvalidGatePlan('invalid stop expiration')
    amount=int(qty)
    return {
        'environment':'GATE_TESTNET', 'execution_enabled':False, 'mode':'ONE_WAY_ISOLATED',
        'entry': {'contract':contract, 'size':amount, 'price':'0', 'tif':'ioc', 'text':client_id},
        'stop': {'initial':{'contract':contract, 'size':0, 'close':True, 'price':'0', 'tif':'ioc', 'reduce_only':True, 'text':'api'},
                 'trigger':{'strategy_type':0, 'price_type':1, 'price':format(stop,'f'),
                            'rule':2 if amount > 0 else 1, 'expiration':expiration_seconds},
                 'order_type':'close-long-position' if amount > 0 else 'close-short-position',
                 'pos_margin_mode':'isolated'},
        'emergency_exit': {'contract':contract, 'size':0, 'close':True, 'price':'0', 'tif':'ioc', 'reduce_only':True},
    }

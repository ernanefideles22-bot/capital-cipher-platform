"""Experimental fail-closed Gate Testnet entry preflight.

Pure validation only. An approval from this function does not authorize an
order; independent ownership, process exclusivity and stop verification must
still be integrated into the executor.
"""
from decimal import Decimal, InvalidOperation


class EntryBlocked(ValueError):
    pass


def _number(value):
    if value is None or isinstance(value,bool):
        raise EntryBlocked('invalid number')
    try:
        n=Decimal(str(value))
    except (ValueError,TypeError,InvalidOperation):
        raise EntryBlocked('invalid number') from None
    if not n.is_finite():
        raise EntryBlocked('invalid number')
    return n


def assess(*, contract, side, quantity, multiplier, price, stop, equity, available,
           config, inventory, central, approval, now):
    if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'} or side not in {'LONG','SHORT'}:
        raise EntryBlocked('invalid market')
    current=_number(now)
    if current <= 0:
        raise EntryBlocked('invalid time')
    def fresh(value, seconds):
        t=_number(value)
        return 0 <= current-t <= seconds
    try:
        if config['mode']!='single' or config['margin']!='isolated' or _number(config['leverage'])!=1 or not fresh(config['observed_at'],10):
            raise EntryBlocked('account configuration')
        if not fresh(inventory['observed_at'],10) or any(type(inventory[k]) is not int or inventory[k] != 0 for k in ('positions','orders','conditional_orders')):
            raise EntryBlocked('account not flat or inventory stale')
        if central['kill_switch_active'] is not False or central['durable_control_active'] is not False or not fresh(central['observed_at'],10):
            raise EntryBlocked('central control blocked')
        if approval['approved'] is not True or approval['status'] not in {'APPROVED','REDUCED'} or not approval['id']:
            raise EntryBlocked('risk approval missing')
        if approval['contract']!=contract or approval['side']!=side or not fresh(approval['observed_at'],30):
            raise EntryBlocked('risk approval stale or mismatched')
        q,m,p,s,e,a=map(_number,(quantity,multiplier,price,stop,equity,available))
        if min(q,m,p,s,e,a) <= 0 or q != q.to_integral_value() or not (s < p if side=='LONG' else s > p):
            raise EntryBlocked('invalid plan')
        notional=q*m*p
        # Conservative two-sided cost allowance: 0.12% per side. It is a
        # budget check, not an estimate of actual execution fees/slippage.
        max_loss=q*m*abs(p-s)+notional*Decimal('0.0024')
        if notional>50 or notional>a or max_loss>e*Decimal('0.001'):
            raise EntryBlocked('risk budget exceeded')
        if notional > _number(approval['max_notional']) or max_loss > _number(approval['max_loss']):
            raise EntryBlocked('risk approval exceeded')
    except (KeyError,TypeError):
        raise EntryBlocked('missing evidence') from None
    return {'preflight_passed':True, 'execution_authorized':False,
            'notional_usdt':str(notional),'budgeted_loss_usdt':str(max_loss)}

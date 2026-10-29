"""Conservative USDT trade outcome calculations for linear contracts.

Inputs must be reconciled fills and settled fees/funding; estimates are not
labelled as realized results. No exchange calls or order execution occur here.
"""
from decimal import Decimal, InvalidOperation


class ResultNotReconciled(ValueError):
    pass


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ResultNotReconciled('missing or invalid value')
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ResultNotReconciled('missing or invalid value') from None
    if not result.is_finite():
        raise ResultNotReconciled('non-finite value')
    return result


def calculate_closed_trade(*, side, entry_fills, exit_fills, contract_multiplier,
                           entry_fees_usdt, exit_fees_usdt, funding_net_usdt):
    """Return net realized PnL; funding is signed, fees are positive costs.

    Only equal fully reconciled entry/exit contract quantities are accepted.
    Each fill has a positive quantity in contracts and a USDT price. Multiplier
    is the base-asset quantity per contract, obtained from the exchange rules.
    """
    if side not in ('LONG', 'SHORT') or not entry_fills or not exit_fills:
        raise ResultNotReconciled('side or fills missing')
    multiplier = _decimal(contract_multiplier)
    entry_fee, exit_fee, funding = map(_decimal, (entry_fees_usdt, exit_fees_usdt, funding_net_usdt))
    if multiplier <= 0 or entry_fee < 0 or exit_fee < 0:
        raise ResultNotReconciled('invalid multiplier or fees')

    def aggregate(fills):
        qty, value = Decimal(0), Decimal(0)
        for row in fills:
            try:
                q, p = _decimal(row['quantity']), _decimal(row['price'])
            except (KeyError, TypeError):
                raise ResultNotReconciled('invalid fill') from None
            if q <= 0 or p <= 0:
                raise ResultNotReconciled('invalid fill')
            qty += q
            value += q * p
        return qty, value

    qty_in, value_in = aggregate(entry_fills)
    qty_out, value_out = aggregate(exit_fills)
    if qty_in != qty_out:
        raise ResultNotReconciled('position not fully closed')
    gross = (value_out - value_in) * multiplier * (1 if side == 'LONG' else -1)
    fees = entry_fee + exit_fee
    net = gross - fees + funding
    return {'quantity_contracts': str(qty_in), 'gross_pnl_usdt': str(gross),
            'fees_usdt': str(fees), 'funding_net_usdt': str(funding),
            'net_pnl_usdt': str(net), 'result': 'WIN' if net > 0 else 'LOSS' if net < 0 else 'FLAT'}

"""Strict conversion of Gate USDT-perpetual candles for offline analysis.

No network access or exchange-order capability is provided by this module.
The caller supplies the response and the instrument's quanto_multiplier.
"""
import math
import time
from decimal import Decimal, InvalidOperation
import httpx

_ORIGIN = 'https://api-testnet.gateapi.io'
_CONTRACTS = {'BTCUSDT':'BTC_USDT', 'ETHUSDT':'ETH_USDT', 'SOLUSDT':'SOL_USDT'}
_INTERVALS = {'1m':60, '5m':300, '15m':900, '1h':3600, '4h':14400}


class GateMarketDataError(ValueError):
    pass


async def fetch_candles(symbol, timeframe, *, limit=200, transport=None, clock=time.time):
    """Fetch only public Testnet futures candles and contract metadata."""
    if symbol not in _CONTRACTS or timeframe not in _INTERVALS or type(limit) is not int or not 60 <= limit <= 1000:
        raise GateMarketDataError('unsupported market request')
    contract = _CONTRACTS[symbol]
    try:
        async with httpx.AsyncClient(base_url=_ORIGIN, timeout=10, follow_redirects=False,
                                     trust_env=False, transport=transport) as client:
            metadata = await client.get('/api/v4/futures/usdt/contracts/' + contract)
            if metadata.status_code != 200:
                raise GateMarketDataError('contract unavailable')
            info = metadata.json()
            if not isinstance(info, dict) or info.get('name') != contract:
                raise GateMarketDataError('contract metadata mismatch')
            response = await client.get('/api/v4/futures/usdt/candlesticks',
                                        params={'contract':contract, 'interval':timeframe, 'limit':limit + 1})
            if response.status_code != 200:
                raise GateMarketDataError('candles unavailable')
            result = normalize_candles(response.json(), interval_seconds=_INTERVALS[timeframe],
                                       now_seconds=clock(), contract_multiplier=info['quanto_multiplier'])
            if len(result) < 60:
                raise GateMarketDataError('insufficient candles')
            return result[-limit:]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        if isinstance(exc, GateMarketDataError):
            raise
        raise GateMarketDataError('market data unavailable') from None


def normalize_candles(rows, *, interval_seconds, now_seconds, contract_multiplier):
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise GateMarketDataError('interval')
    try:
        multiplier = Decimal(str(contract_multiplier))
        now = Decimal(str(now_seconds))
    except (InvalidOperation, ValueError):
        raise GateMarketDataError('metadata') from None
    if not multiplier.is_finite() or multiplier <= 0 or not now.is_finite() or now <= 0:
        raise GateMarketDataError('metadata')
    if not isinstance(rows, list) or not rows:
        raise GateMarketDataError('candles')
    output = []
    for row in rows:
        try:
            start = Decimal(str(row['t']))
            if not start.is_finite() or start != start.to_integral_value():
                raise ValueError()
            start = int(start)
            if start <= 0 or start % interval_seconds:
                raise ValueError()
            nums = [Decimal(str(row[k])) for k in ('o', 'h', 'l', 'c', 'v')]
            if any(not n.is_finite() for n in nums):
                raise ValueError()
            op, high, low, close, volume = nums
            if min(op, high, low, close) <= 0 or volume < 0 or high < max(op, close) or low > min(op, close) or high < low:
                raise ValueError()
            values = [float(x) for x in (op, high, low, close, volume * multiplier)]
            if not all(math.isfinite(x) for x in values):
                raise ValueError()
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            raise GateMarketDataError('invalid candle') from None
        if start + interval_seconds <= now:
            output.append({'close_time_ms': (start + interval_seconds) * 1000,
                           'open': values[0], 'high': values[1], 'low': values[2],
                           'close': values[3], 'volume': values[4]})
    output.sort(key=lambda item: item['close_time_ms'])
    if not output or any(b['close_time_ms'] - a['close_time_ms'] != interval_seconds * 1000
                         for a, b in zip(output, output[1:])):
        raise GateMarketDataError('missing or duplicate candles')
    if now - Decimal(output[-1]['close_time_ms']) / 1000 >= interval_seconds * 2:
        raise GateMarketDataError('stale candles')
    return output

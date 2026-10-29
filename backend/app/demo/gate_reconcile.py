"""Read-only, bounded Gate Testnet futures account inventory.

The three reads are not atomic. Their result is diagnostic, not permission
to place an order or proof that no concurrent account activity occurred.
"""
import hashlib
import hmac
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

_ORIGIN='https://api-testnet.gateapi.io'
_PATHS=('/api/v4/futures/usdt/positions', '/api/v4/futures/usdt/orders',
        '/api/v4/futures/usdt/price_orders')


class ReconcileError(Exception):
    pass


async def inventory(key, secret, *, transport=None, clock=time.time):
    if not key or not secret:
        raise ReconcileError('credentials missing')
    async def get(client, path, query):
        timestamp=str(int(clock()))
        text=urlencode(query)
        empty=hashlib.sha512(b'').hexdigest()
        signed=f'GET\n{path}\n{text}\n{empty}\n{timestamp}'
        signature=hmac.new(secret.encode(), signed.encode(), hashlib.sha512).hexdigest()
        response=await client.get(_ORIGIN+path,params=query,headers={'KEY':key,'Timestamp':timestamp,'SIGN':signature})
        if response.status_code != 200:
            raise ReconcileError('exchange request failed')
        result=response.json()
        if not isinstance(result,list):
            raise ReconcileError('unexpected response')
        return result
    try:
        async with httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False,transport=transport) as client:
            positions=await get(client,_PATHS[0],[('holding','true')])
            orders=await get(client,_PATHS[1],[('status','open'),('limit','100')])
            stops=await get(client,_PATHS[2],[('status','open'),('limit','100')])
        # Fail closed on a full page: completeness cannot be established.
        if len(orders) >= 100 or len(stops) >= 100:
            raise ReconcileError('inventory exceeds single page')
        open_positions=0
        for row in positions:
            if not isinstance(row,dict) or 'size' not in row or isinstance(row['size'],bool):
                raise ReconcileError('invalid position')
            size=Decimal(str(row['size']))
            if not size.is_finite():
                raise ReconcileError('invalid position')
            if size != 0:
                open_positions += 1
        if any(not isinstance(row,dict) or row.get('status') != 'open' for row in orders+stops):
            raise ReconcileError('invalid order inventory')
        return {'positions':open_positions,'orders':len(orders),'conditional_orders':len(stops),
                'atomic':False, 'execution_authorized':False}
    except (httpx.HTTPError, ValueError, TypeError, InvalidOperation):
        raise ReconcileError('inventory unavailable') from None

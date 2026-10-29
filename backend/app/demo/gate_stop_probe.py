"""Read-only bounded Gate Testnet conditional-order confirmation.

Does not submit orders or authorize execution. Caller must verify position
ownership and use a synchronized trusted clock before relying on a result.
"""
import asyncio
import hashlib
import hmac
import re
import time

import httpx
from app.demo.gate_stop_validation import stop_matches

_HOST='https://api-testnet.gateapi.io'
_BASE='/api/v4/futures/usdt/price_orders/'


async def read_verified_stop(key, secret, *, order_id, contract, position_size, trigger_price,
                             transport=None, clock=time.time, sleep=asyncio.sleep):
    """Return the validated provider snapshot and its observation time, or None.

    The caller must independently reconcile the position and revalidate the
    evidence at consumption time. This read never authorizes execution.
    """
    if not key or not secret or not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
        return None
    if contract not in {'BTC_USDT','ETH_USDT','SOL_USDT'}:
        return None
    path=_BASE+order_id
    async with httpx.AsyncClient(timeout=8,follow_redirects=False,trust_env=False,transport=transport) as client:
        for attempt in range(3):
            timestamp=str(int(clock()))
            digest=hashlib.sha512(b'').hexdigest()
            message=f'GET\n{path}\n\n{digest}\n{timestamp}'
            signature=hmac.new(secret.encode(),message.encode(),hashlib.sha512).hexdigest()
            try:
                response=await client.get(_HOST+path,headers={'KEY':key,'Timestamp':timestamp,'SIGN':signature})
                observed=clock()
                if response.status_code==200:
                    row=response.json()
                    if stop_matches(row,order_id=order_id,contract=contract,position_size=position_size,
                                    trigger_price=trigger_price,observed_at=observed,now=clock()):
                        return row, observed
            except (httpx.HTTPError,ValueError,TypeError):
                pass
            if attempt < 2:
                await sleep(.5)
    return None


async def confirm_stop(key, secret, **kwargs):
    """Compatibility wrapper for callers that need only a boolean."""
    return (await read_verified_stop(key, secret, **kwargs)) is not None


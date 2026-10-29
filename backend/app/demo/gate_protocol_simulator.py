"""Mock-only Gate Testnet protocol harness; no live keys or network access.

Not an executor. It helps test exact signing and ambiguity handling before
the exchange adapter is implemented and approved for Testnet.
"""
import hashlib
import hmac
import json
import re

import httpx

_ORIGIN='https://api-testnet.gateapi.io'
_ORDER='/api/v4/futures/usdt/orders'
_STOP='/api/v4/futures/usdt/price_orders'
_KEY='test-key'
_SECRET='test-secret'


class ExchangeOutcomeUnknown(Exception):
    """A write may have happened; reconcile independently before any retry."""


class SimulatedProtocolError(Exception):
    pass


class GateProtocolSimulator:
    def __init__(self, transport, *, clock):
        if type(transport) is not httpx.MockTransport:
            raise SimulatedProtocolError('mock transport required')
        self._transport=transport
        self._clock=clock

    async def _request(self, method, path, *, body=None):
        if method not in {'GET','POST'}:
            raise SimulatedProtocolError('blocked method')
        if method=='POST' and path not in {_ORDER,_STOP}:
            raise SimulatedProtocolError('blocked path')
        if method=='GET' and not re.fullmatch(r'/api/v4/futures/usdt/(orders|price_orders)/[1-9][0-9]*',path):
            raise SimulatedProtocolError('blocked path')
        payload=b'' if body is None else json.dumps(body,separators=(',',':'),allow_nan=False).encode()
        stamp=str(int(self._clock()))
        digest=hashlib.sha512(payload).hexdigest()
        message=f'{method}\n{path}\n\n{digest}\n{stamp}'
        signature=hmac.new(_SECRET.encode(),message.encode(),hashlib.sha512).hexdigest()
        headers={'KEY':_KEY,'Timestamp':stamp,'SIGN':signature,'Content-Type':'application/json'}
        try:
            async with httpx.AsyncClient(transport=self._transport,follow_redirects=False,trust_env=False,timeout=8) as client:
                response=await client.request(method,_ORIGIN+path,content=payload,headers=headers)
            if response.status_code not in ({200,201} if method=='POST' else {200}):
                raise ValueError('unexpected status')
            result=response.json()
            if not isinstance(result,dict):
                raise ValueError('unexpected body')
            return result
        except (httpx.HTTPError,ValueError,TypeError):
            if method=='POST':
                raise ExchangeOutcomeUnknown('write outcome unknown; reconcile without retry') from None
            raise SimulatedProtocolError('read unavailable') from None

    async def submit_entry(self, plan):
        return await self._request('POST',_ORDER,body=plan['entry'])

    async def submit_stop(self, plan):
        return await self._request('POST',_STOP,body=plan['stop'])

    async def submit_exit(self, plan):
        return await self._request('POST',_ORDER,body=plan['emergency_exit'])

    async def get_stop(self, order_id):
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            raise SimulatedProtocolError('invalid order id')
        return await self._request('GET',_STOP+'/'+order_id)

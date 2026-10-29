"""Unwired Gate Testnet order transport; writes are disabled by default.

This is not an executor or risk control. No live trading host can be supplied.
Callers must persist intents, reconcile positions and stops, enforce account
and central risk controls, and prove protections before allowing writes.
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import httpx

from app.demo.gate_order_plan import build_order_plan

_HOST='https://api-testnet.gateapi.io'
_ORDER='/api/v4/futures/usdt/orders'
_STOP='/api/v4/futures/usdt/price_orders'
_ACK='GATE_TESTNET_VIRTUAL_FUNDS_ONLY'
_CONTRACTS={'BTC_USDT','ETH_USDT','SOL_USDT'}


class GateReadError(Exception):
    pass


class GateWriteUnknown(Exception):
    """A write may have reached the exchange; reconcile before any action."""


class GateWriteDisabled(Exception):
    pass


@dataclass(frozen=True,repr=False)
class TestnetCredentials:
    key: str
    secret: str
    def __post_init__(self):
        if not self.key or not self.secret or any(c.isspace() for c in self.key+self.secret):
            raise ValueError('invalid testnet credentials')
    def __repr__(self):
        return 'TestnetCredentials(***)'


class GateTestnetTransport:
    def __init__(self, credentials, *, trusted_clock, writes_enabled=False, acknowledgement='', transport=None):
        if not isinstance(credentials,TestnetCredentials) or not callable(trusted_clock):
            raise ValueError('credentials and trusted clock required')
        self._credentials=credentials
        self._clock=trusted_clock
        self._writes_enabled=writes_enabled is True and acknowledgement==_ACK
        self._client=httpx.AsyncClient(base_url=_HOST,timeout=8,follow_redirects=False,trust_env=False,transport=transport)

    async def close(self):
        await self._client.aclose()

    async def _request(self, method, path, *, body=None):
        write=method in {'POST','DELETE'}
        if write and not self._writes_enabled:
            raise GateWriteDisabled('testnet writes disabled')
        if method=='POST':
            valid=path in {_ORDER,_STOP}
        elif method=='DELETE':
            valid=bool(re.fullmatch(re.escape(_STOP)+r'/[1-9][0-9]*',path))
        elif method=='GET':
            valid=bool(re.fullmatch(re.escape(_ORDER)+r'/(?:[1-9][0-9]*|t-[A-Za-z0-9_.-]{1,28})',path)
                       or re.fullmatch(re.escape(_STOP)+r'/[1-9][0-9]*',path)
                       or path in {'/api/v4/futures/usdt/positions/'+c for c in _CONTRACTS})
        else:
            valid=False
        if not valid:
            raise ValueError('blocked endpoint')
        payload=b'' if body is None else json.dumps(body,separators=(',',':'),allow_nan=False).encode()
        t=self._clock()
        if isinstance(t,bool) or not isinstance(t,(int,float)) or not (0 < t < 100000000000):
            raise ValueError('untrusted clock')
        timestamp=str(int(t))
        digest=hashlib.sha512(payload).hexdigest()
        signature=hmac.new(self._credentials.secret.encode(),f'{method}\n{path}\n\n{digest}\n{timestamp}'.encode(),hashlib.sha512).hexdigest()
        headers={'KEY':self._credentials.key,'Timestamp':timestamp,'SIGN':signature,'Accept':'application/json','Content-Type':'application/json'}
        try:
            result=await self._client.request(method,_HOST+path,content=payload,headers=headers)
            if result.status_code not in ({200,201} if write else {200}):
                raise ValueError('unexpected provider status')
            data=result.json()
            if not isinstance(data,dict):
                raise ValueError('unexpected provider response')
            if write:
                order_id=str(data.get('id_string') or data.get('id') or '')
                if not re.fullmatch(r'[1-9][0-9]*',order_id):
                    raise ValueError('provider id missing')
                return {'id':order_id}
            return data
        except (httpx.HTTPError,ValueError,TypeError):
            if write:
                raise GateWriteUnknown('write outcome unknown; reconcile without retry') from None
            raise GateReadError('exchange read unavailable') from None

    async def place_entry(self, *, contract, signed_contracts, client_id):
        if contract not in _CONTRACTS or type(signed_contracts) is not int or not 0 < abs(signed_contracts) <= 1000000:
            raise ValueError('invalid entry')
        if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
            raise ValueError('invalid client id')
        return await self._request('POST',_ORDER,body={'contract':contract,'size':signed_contracts,'price':'0','tif':'ioc','text':client_id})

    async def place_stop(self, *, contract, signed_position, reference_price, stop_price, tick_size):
        plan=build_order_plan(contract=contract,signed_contracts=signed_position,mark_price=reference_price,
                              stop_price=stop_price,tick_size=tick_size,client_id='t-stop-plan')
        return await self._request('POST',_STOP,body=plan['stop'])

    async def close_position(self, *, contract):
        if contract not in _CONTRACTS:
            raise ValueError('invalid contract')
        return await self._request('POST',_ORDER,body={'contract':contract,'size':0,'close':True,'price':'0','tif':'ioc','reduce_only':True})

    async def get_order(self, order_id):
        if not isinstance(order_id,str) or not re.fullmatch(r'(?:[1-9][0-9]*|t-[A-Za-z0-9_.-]{1,28})',order_id):
            raise ValueError('invalid order id')
        return await self._request('GET',_ORDER+'/'+order_id)

    async def get_position(self, contract):
        """Read one Testnet position; no account mutation or permission to trade."""
        if contract not in _CONTRACTS:
            raise ValueError('invalid contract')
        return await self._request('GET','/api/v4/futures/usdt/positions/'+contract)

    async def get_stop(self, order_id):
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            raise ValueError('invalid stop id')
        return await self._request('GET',_STOP+'/'+order_id)

    async def cancel_stop(self, order_id):
        if not isinstance(order_id,str) or not re.fullmatch(r'[1-9][0-9]*',order_id):
            raise ValueError('invalid stop id')
        return await self._request('DELETE',_STOP+'/'+order_id)


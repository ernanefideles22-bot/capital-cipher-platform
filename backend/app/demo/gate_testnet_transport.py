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
from urllib.parse import urlencode
import httpx

from app.demo.gate_order_plan import build_order_plan

_HOST='https://api-testnet.gateapi.io'
_ORDER='/api/v4/futures/usdt/orders'
_STOP='/api/v4/futures/usdt/price_orders'
_ACK='GATE_TESTNET_VIRTUAL_FUNDS_ONLY'
_CONTRACTS={'BTC_USDT','ETH_USDT','SOL_USDT'}
_ERROR_FIELDS=('initial', 'trigger', 'contract', 'size', 'amount', 'close',
               'reduce_only', 'tif', 'text', 'price', 'price_type', 'strategy_type',
               'rule', 'expiration', 'order_type', 'pos_margin_mode', 'last_price')
_RESPONSE_KINDS={'json_object','json_other','invalid_json','oversized'}


def _safe_error_label(value, forbidden=()):
    # Gate adds error codes independently of this client. Keep bounded enum-like
    # labels rather than silently dropping every new code. Never keep free text
    # or a value reflecting this request's credentials/signature.
    if not isinstance(value,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}',value):
        return None
    if any(secret and (secret.casefold() in value.casefold() or value.casefold() in secret.casefold())
           for secret in forbidden):
        return None
    return value


class GateReadError(Exception):
    pass


class GateWriteUnknown(Exception):
    """A write may have reached the exchange; reconcile before any action."""

    def __init__(self, message='write outcome unknown; reconcile without retry', *, http_status=None, provider_label=None,
                 provider_fields=(), response_kind=None):
        super().__init__('write outcome unknown; reconcile without retry')
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.provider_label = _safe_error_label(provider_label)
        self.provider_fields = tuple(field for field in _ERROR_FIELDS if field in provider_fields)
        self.response_kind = response_kind if response_kind in _RESPONSE_KINDS else None


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

    async def _request(self, method, path, *, body=None, query=None, expect_list=False):
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
                       or path in {'/api/v4/futures/usdt/positions/'+c for c in _CONTRACTS}
                       or path in {'/api/v4/futures/usdt/accounts','/api/v4/futures/usdt/positions',_ORDER,_STOP,'/api/v4/futures/usdt/my_trades'})
        else:
            valid=False
        if not valid:
            raise ValueError('blocked endpoint')
        query_text=urlencode(query or [])
        payload=b'' if body is None else json.dumps(body,separators=(',',':'),allow_nan=False).encode()
        t=self._clock()
        if isinstance(t,bool) or not isinstance(t,(int,float)) or not (0 < t < 100000000000):
            raise ValueError('untrusted clock')
        timestamp=str(int(t))
        digest=hashlib.sha512(payload).hexdigest()
        signature=hmac.new(self._credentials.secret.encode(),f'{method}\n{path}\n{query_text}\n{digest}\n{timestamp}'.encode(),hashlib.sha512).hexdigest()
        headers={'KEY':self._credentials.key,'Timestamp':timestamp,'SIGN':signature,'Accept':'application/json','Content-Type':'application/json'}
        result=None
        provider_label=None
        provider_fields=()
        response_kind=None
        try:
            result=await self._client.request(method,_HOST+path+('?' + query_text if query_text else ''),content=payload,headers=headers)
            if result.status_code not in ({200,201} if write else {200}):
                # Preserve a bounded code and finite field hints, never messages.
                if write and len(result.content)<=2_000_000:
                    try:
                        error=result.json()
                        response_kind='json_object' if isinstance(error,dict) else 'json_other'
                        if isinstance(error,dict):
                            provider_label=_safe_error_label(error.get('label'),
                                (self._credentials.key,self._credentials.secret,signature))
                            # Only vocabulary from the request schema can leave
                            # this scope; values and provider prose cannot.
                            messages=' '.join(value[:4096] for key in ('message','detail')
                                              if isinstance(value:=error.get(key),str))
                            provider_fields=tuple(field for field in _ERROR_FIELDS
                                if re.search(r'(?<![\w])'+re.escape(field)+r'(?![\w])',messages,re.IGNORECASE))
                    except (ValueError,TypeError):
                        response_kind='invalid_json'
                elif write:
                    response_kind='oversized'
                raise ValueError('unexpected provider status')
            if len(result.content) > 2_000_000:
                raise ValueError('oversized provider response')
            data=result.json()
            if not isinstance(data,list if expect_list else dict):
                raise ValueError('unexpected provider response')
            if write:
                order_id=str(data.get('id_string') or data.get('id') or '')
                if not re.fullmatch(r'[1-9][0-9]*',order_id):
                    raise ValueError('provider id missing')
                return {'id':order_id}
            return data
        except (httpx.HTTPError,ValueError,TypeError):
            if write:
                raise GateWriteUnknown(http_status=result.status_code if result is not None else None,
                                       provider_label=provider_label,provider_fields=provider_fields,
                                       response_kind=response_kind) from None
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

    async def close_position(self, *, contract, client_id=None):
        if contract not in _CONTRACTS:
            raise ValueError('invalid contract')
        body={'contract':contract,'size':0,'close':True,'price':'0','tif':'ioc','reduce_only':True}
        if client_id is not None:
            if not isinstance(client_id,str) or not re.fullmatch(r't-[A-Za-z0-9_.-]{1,28}',client_id):
                raise ValueError('invalid client id')
            body['text']=client_id
        return await self._request('POST',_ORDER,body=body)

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


    async def account(self):
        return await self._request('GET','/api/v4/futures/usdt/accounts')

    async def rows(self, kind, *, contract=None, status=None):
        paths={'positions':'positions','orders':'orders','stops':'price_orders','fills':'my_trades'}
        if kind not in paths or (contract is not None and contract not in _CONTRACTS):
            raise ValueError('invalid inventory scope')
        query=[]
        if contract is not None: query.append(('contract',contract))
        if status is not None:
            if kind not in {'orders','stops'} or status not in {'open','finished'}:
                raise ValueError('invalid inventory status')
            query.append(('status',status))
        path='/api/v4/futures/usdt/'+paths[kind]
        if kind == 'positions':
            data=await self._request('GET',path,query=query,expect_list=True)
            if any(not isinstance(row,dict) for row in data):
                raise GateReadError('invalid position inventory')
            return data
        query.append(('limit','100'))
        collected,seen=[],set()
        # Never silently truncate a full page. A bounded scan that cannot prove
        # completeness is unavailable; it must not report an empty account.
        for offset in range(0,2000,100):
            page=await self._request('GET',path,query=query+[('offset',str(offset))],expect_list=True)
            if len(page)>100 or any(not isinstance(row,dict) for row in page):
                raise GateReadError('invalid inventory page')
            for row in page:
                oid=str(row.get('id_string') or row.get('id') or '')
                if not re.fullmatch(r'[1-9][0-9]*',oid) or oid in seen:
                    raise GateReadError('inventory changed or invalid identity')
                seen.add(oid)
            collected.extend(page)
            if len(page)<100:
                return collected
        raise GateReadError('inventory incomplete; reconciliation checkpoint required')


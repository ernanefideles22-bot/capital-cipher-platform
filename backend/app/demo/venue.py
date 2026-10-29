"""Exact-host, single-attempt USD-M Futures Demo execution transport."""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

import httpx

from app.demo.binance import BinanceFuturesDemoClient, DEMO_ORIGIN, DemoError

PUBLIC = frozenset({'/fapi/v1/time', '/fapi/v1/exchangeInfo', '/fapi/v1/premiumIndex', '/fapi/v1/klines'})
SIGNED = {
    'GET': frozenset({'/fapi/v1/order', '/fapi/v1/algoOrder', '/fapi/v1/openOrders', '/fapi/v1/openAlgoOrders', '/fapi/v1/userTrades', '/fapi/v1/symbolConfig'}),
    'POST': frozenset({'/fapi/v1/order', '/fapi/v1/algoOrder', '/fapi/v1/leverage', '/fapi/v1/marginType'}),
    'DELETE': frozenset({'/fapi/v1/order', '/fapi/v1/algoOrder'}),
}


class VenueError(DemoError):
    def __init__(self, code, *, ambiguous=False, venue_code=None):
        super().__init__(code)
        self.ambiguous = ambiguous
        self.venue_code = venue_code


class FuturesDemoVenue(BinanceFuturesDemoClient):
    async def request(self, method, path, params=None, *, public=False):
        if (public and (method != 'GET' or path not in PUBLIC)) or (not public and path not in SIGNED.get(method, ())):
            raise VenueError('DEMO_ENDPOINT_BLOCKED')
        pairs = [(k, str(v)) for k, v in (params or {}).items()]
        headers = {}
        write = method != 'GET'
        if not public:
            if not self._credentials or not self._clock_synchronized:
                raise VenueError('DEMO_CREDENTIAL_OR_CLOCK_MISSING')
            pairs += [('recvWindow', '5000'), ('timestamp', str(self._clock_ms()+self._offset_ms))]
            signature = hmac.new(self._credentials.signing_secret.encode(), urlencode(pairs).encode(), hashlib.sha256).hexdigest()
            pairs.append(('signature', signature))
            headers['X-MBX-APIKEY'] = self._credentials.key_id
        try:
            response = await self._client.request(method, DEMO_ORIGIN+path, params=pairs, headers=headers)
        except httpx.HTTPError:
            raise VenueError('DEMO_WRITE_UNKNOWN' if write else 'DEMO_NETWORK_UNAVAILABLE', ambiguous=write) from None
        if response.is_redirect:
            raise VenueError('DEMO_REDIRECT_BLOCKED', ambiguous=write)
        try:
            data = response.json()
        except ValueError:
            raise VenueError('DEMO_RESPONSE_INVALID', ambiguous=write) from None
        code = data.get('code') if isinstance(data, dict) else None
        negative = isinstance(code, int) and code < 0
        if response.status_code >= 400 or negative:
            ambiguous = write and (response.status_code >= 500 or code in {-1006, -1007})
            raise VenueError('DEMO_WRITE_UNKNOWN' if ambiguous else 'DEMO_VENUE_REJECTED', ambiguous=ambiguous, venue_code=code)
        return data

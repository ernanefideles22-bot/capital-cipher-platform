"""Read public contract metadata from the fixed Gate Testnet endpoint."""
import math
import time

import httpx


class GateMetadataUnavailable(ValueError):
    pass


class GateTestnetMetadata:
    _CONTRACTS={"BTCUSDT":"BTC_USDT","ETHUSDT":"ETH_USDT","SOLUSDT":"SOL_USDT"}
    _ORIGIN="https://api-testnet.gateapi.io"

    def __init__(self, *, transport=None, clock=time.time):
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock=clock
        self._client=httpx.AsyncClient(base_url=self._ORIGIN,timeout=8,follow_redirects=False,
                                       trust_env=False,transport=transport)

    async def __call__(self, symbol):
        contract=self._CONTRACTS.get(symbol)
        if contract is None:
            raise GateMetadataUnavailable("unsupported contract")
        try:
            response=await self._client.get("/api/v4/futures/usdt/contracts/"+contract)
            if response.status_code != 200 or len(response.content)>65536:
                raise ValueError()
            payload=response.json()
            if not isinstance(payload,dict) or payload.get("name")!=contract:
                raise ValueError()
            result={key:payload[key] for key in ("name","quanto_multiplier","order_size_min",
                                                  "order_size_max","in_delisting")}
            for key in ("order_price_round", "mark_price"):
                if key in payload:
                    result[key] = payload[key]
            observed=self._clock()
            if isinstance(observed,bool) or not isinstance(observed,(int,float)) or not math.isfinite(observed) or observed <= 0:
                raise ValueError()
            return result,observed
        except (httpx.HTTPError,KeyError,TypeError,ValueError,OverflowError):
            raise GateMetadataUnavailable("testnet metadata unavailable") from None

    async def aclose(self):
        await self._client.aclose()

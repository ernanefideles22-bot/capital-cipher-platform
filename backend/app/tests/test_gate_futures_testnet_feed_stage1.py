"""On-demand Gate Futures TestNet public GET contract; no broker access."""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.strategy.gate_futures_testnet_feed_stage1 import (
    GATE_FUTURES_TESTNET_BASE,
    fetch_gate_testnet_chart,fetch_gate_testnet_multi_timeframe,
    normalize_gate_futures_candles,
)
from app.strategy.professional_gate_chart_stage1 import BAR_SECONDS


def rows(tf="15m",size=100,*,include_open_bar=False):
    step=BAR_SECONDS[tf]
    now=int(datetime.now(timezone.utc).timestamp())
    current_open=(now//step)*step
    payload=[]
    for index in range(size):
        opened=current_open-(size-index)*step
        price=100+0.15*index
        payload.append({
            "t":opened,"o":str(price),"h":str(price+1),
            "l":str(price-1),"c":str(price+0.3),"v":"35",
        })
    if include_open_bar:
        payload.append({
            "t":current_open,"o":"5000","h":"5001",
            "l":"4999","c":"5000","v":"2",
        })
    return payload


def test_gate_normalizer_drops_open_bar_and_uses_actual_close_time():
    source=rows(include_open_bar=True)
    now=datetime.now(timezone.utc)
    candles=normalize_gate_futures_candles(
        source,contract="BTC_USDT",timeframe="15m",received_at=now,
    )
    assert len(candles)==100
    assert candles[-1].symbol=="BTC_USDT"
    assert candles[-1].exchange.value=="GATE"
    assert int(candles[-1].closed_at.timestamp())==source[-2]["t"]+900
    assert all(x.received_at==now for x in candles)
    assert candles[-1].close!=5000


def test_duplicate_bar_and_wrong_market_fail_closed():
    source=rows()
    with pytest.raises(ValueError,match="Duplicate"):
        normalize_gate_futures_candles(
            source+[source[-1]],contract="BTC_USDT",timeframe="15m",
            received_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValueError,match="Unapproved"):
        normalize_gate_futures_candles(
            source,contract="BTCUSDT",timeframe="15m",
            received_at=datetime.now(timezone.utc),
        )


def test_missing_and_misaligned_bar_not_repaired():
    source=rows()
    bad=[x for i,x in enumerate(source) if i!=15]
    bars=normalize_gate_futures_candles(
        bad,contract="BTC_USDT",timeframe="15m",
        received_at=datetime.now(timezone.utc),
    )
    assert len(bars)==99
    source[15]={**source[15],"t":source[15]["t"]+12}
    with pytest.raises(ValueError,match="Unusable"):
        normalize_gate_futures_candles(
            source,contract="BTC_USDT",timeframe="15m",
            received_at=datetime.now(timezone.utc),
        )


def test_no_live_base_no_auth_and_get_only():
    requests=[]
    def handler(req:httpx.Request):
        requests.append(req)
        return httpx.Response(200,json=rows("15m",90,include_open_bar=True))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_gate_testnet_chart(
                client,contract="BTC_USDT",timeframe="15m",requested_limit=90,
            )
    out=asyncio.run(run())
    assert len(requests)==1
    request=requests[0]
    assert request.method=="GET"
    assert str(request.url).startswith(GATE_FUTURES_TESTNET_BASE)
    assert "/futures/usdt/candlesticks" in str(request.url)
    assert request.url.params["contract"]=="BTC_USDT"
    assert request.url.params["interval"]=="15m"
    assert "Authorization" not in request.headers
    assert "KEY" not in request.headers
    assert "SIGN" not in request.headers
    assert out.candles_analyzed==90
    assert not out.trading_authorized
    assert not out.data_source_authenticated


def test_multi_tf_adapter_fetches_gate_only_and_does_not_place_orders():
    methods=[]
    def handler(req:httpx.Request):
        methods.append((req.method,str(req.url)))
        return httpx.Response(200,json=rows(req.url.params["interval"],80))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_gate_testnet_multi_timeframe(
                client,contract="ETH_USDT",requested_limit=80,
            )
    result=asyncio.run(run())
    assert len(methods)==3
    assert all(method=="GET" and GATE_FUTURES_TESTNET_BASE in url
               for method,url in methods)
    assert set(next(z for z in ["15m","1h","4h"] if f"interval={z}" in url)
               for _,url in methods)=={"15m","1h","4h"}
    assert result.symbol=="ETH_USDT"
    assert result.alignment=="BULLISH_ALIGNMENT"
    assert not result.trading_authorized


def test_stale_empty_http_errors_and_bad_json_do_not_trigger_fallback():
    async def request(body,status=200):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req:httpx.Response(status,json=body))) as client:
            return await fetch_gate_testnet_chart(
                client,contract="BTC_USDT",timeframe="15m",requested_limit=100,
            )
    async def invalid_limit():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(200,json=rows()))) as client:
            await fetch_gate_testnet_chart(
                client,contract="BTC_USDT",timeframe="15m",requested_limit=30,
            )
    with pytest.raises(ValueError,match="80-300"):
        asyncio.run(invalid_limit())
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(request({},status=500))
    with pytest.raises(ValueError,match="Invalid"):
        asyncio.run(request({"unexpected":"body"}))
    with pytest.raises(ValueError,match="80-3000"):
        asyncio.run(request([]))

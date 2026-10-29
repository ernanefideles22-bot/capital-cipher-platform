import httpx
import pytest
from app.demo.gate_market import normalize_candles, GateMarketDataError, fetch_candles


def candle(t):
    return {'t':t,'o':'100','h':'103','l':'98','c':'102','v':'25'}


def normalize(rows, **kwargs):
    settings={'interval_seconds':60, 'now_seconds':180, 'contract_multiplier':'0.001'}
    settings.update(kwargs)
    return normalize_candles(rows, **settings)


def test_converts_contract_volume_and_excludes_open_candle():
    rows=normalize([candle(180),candle(120),candle(60)])
    assert [r['close_time_ms'] for r in rows] == [120000,180000]
    assert rows[0]['volume'] == .025


@pytest.mark.parametrize('rows', [[], [candle(60),candle(60)], [candle(60),candle(180)],
                                   [{**candle(60),'v':'NaN'}], [{**candle(60),'h':'90'}]])
def test_rejects_bad_input(rows):
    with pytest.raises(GateMarketDataError):
        normalize(rows, now_seconds=240)


def test_rejects_stale_data_and_bad_multiplier():
    with pytest.raises(GateMarketDataError):
        normalize([candle(60)], now_seconds=300)
    with pytest.raises(GateMarketDataError):
        normalize([candle(60)], contract_multiplier=0)


@pytest.mark.asyncio
async def test_fetch_is_public_testnet_only_and_converts_candles():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.host == 'api-testnet.gateapi.io'
        assert request.method == 'GET' and 'KEY' not in request.headers
        if request.url.path.endswith('/contracts/BTC_USDT'):
            return httpx.Response(200, json={'name':'BTC_USDT','quanto_multiplier':'0.001'})
        assert request.url.path == '/api/v4/futures/usdt/candlesticks'
        assert request.url.params['contract'] == 'BTC_USDT'
        return httpx.Response(200, json=[candle(t) for t in range(60, 3720, 60)])
    rows=await fetch_candles('BTCUSDT', '1m', limit=60,
                             transport=httpx.MockTransport(handler), clock=lambda:3660)
    assert len(rows)==60 and rows[-1]['close_time_ms']==3660000
    assert rows[-1]['volume']==.025 and len(calls)==2


@pytest.mark.asyncio
async def test_refuses_redirects_and_unsupported_symbols():
    with pytest.raises(GateMarketDataError):
        await fetch_candles('DOGEUSDT', '1m')
    transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={'location':'https://example.com'}))
    with pytest.raises(GateMarketDataError):
        await fetch_candles('BTCUSDT', '1m', limit=60, transport=transport)

import httpx
import pytest
from app.demo.gate_position_config import interpret_position_config, read_position_config, ConfigUnavailable


def position(**updates):
    result={'contract':'BTC_USDT','mode':'single','size':'0','leverage':'1',
            'pos_margin_mode':'isolated','lever':'1','cross_leverage_limit':'0'}
    result.update(updates)
    return result


def test_isolated_one_times_is_identified_but_never_authorizes_execution():
    result=interpret_position_config(position())
    assert result['one_way_isolated_1x'] and result['execution_authorized'] is False


def test_cross_76x_is_not_confused_with_isolated():
    result=interpret_position_config(position(leverage='0',pos_margin_mode='cross',lever='76',cross_leverage_limit='76'))
    assert result['margin']=='cross' and result['leverage']=='76' and not result['one_way_isolated_1x']


@pytest.mark.parametrize('data',[
    position(contract='ETH_USDT'),position(mode='unsupported'),position(leverage='NaN'),
    position(leverage='0',pos_margin_mode='isolated'),position(leverage='5',lever='1'),
    position(leverage='1',pos_margin_mode='cross'),position(lever='NaN'),position(size=True),
])
def test_ambiguous_config_fails_closed(data):
    with pytest.raises(ConfigUnavailable): interpret_position_config(data)


@pytest.mark.asyncio
async def test_only_reads_testnet_and_does_not_follow_redirect():
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET' and request.url.host=='api-testnet.gateapi.io'
        assert request.url.path=='/api/v4/futures/usdt/positions/BTC_USDT'
        return httpx.Response(200,json=position())
    result=await read_position_config('key','secret',transport=httpx.MockTransport(handler),clock=lambda:1000)
    assert result['one_way_isolated_1x'] and len(calls)==1
    with pytest.raises(ConfigUnavailable):
        await read_position_config('key','secret',transport=httpx.MockTransport(lambda req:httpx.Response(302,headers={'location':'https://example.com'})))

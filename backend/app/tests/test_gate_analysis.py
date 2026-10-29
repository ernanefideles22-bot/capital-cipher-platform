import os
import pytest
from app.demo.gate_analysis import analyze_snapshot, GateAnalysisError


@pytest.mark.asyncio
async def test_gate_snapshot_runs_cohort_without_live_order_capability(monkeypatch):
    monkeypatch.setenv('CAPITAL_CIPHER_GATE_TESTNET_API_SECRET', 'must-not-leak')
    async def fake_market(symbol, timeframe, limit):
        assert (symbol,timeframe,limit)==('BTCUSDT','15m',200)
        return [{'close_time_ms':(i+1)*900000,'open':100.,'high':102.,'low':99.,'close':101.,'volume':5.}
                for i in range(200)]
    result=await analyze_snapshot('BTCUSDT','15m',1000,fetch=fake_market)
    assert result['exchange']=='GATE' and result['market']=='GATE_USDT_FUTURES_TESTNET'
    assert result['analysis_scope']=='RESEARCH_ONLY' and result['execution_enabled'] is False
    assert result['total_agents'] >= 3 and 'must-not-leak' not in str(result)
    primary={a['agent_name']:a for a in result['agents']}
    assert primary['MarketDataAgent']['status']=='COMPLETED'
    assert primary['QuantAgent']['status']=='COMPLETED'
    assert primary['TrendAgent']['status']=='COMPLETED'


@pytest.mark.asyncio
async def test_invalid_equity_cannot_trigger_market_fetch():
    async def bad_fetch(*args, **kwargs):
        raise AssertionError('unexpected fetch')
    with pytest.raises(GateAnalysisError):
        await analyze_snapshot('BTCUSDT','15m',float('nan'),fetch=bad_fetch)

"""Stage 1 chart reader exposed to isolated SHADOW agents; no order path."""
import asyncio
from datetime import datetime,timedelta,timezone

from app.agents.gate_professional_chart_shadow import GateProfessionalChartShadowAgent
from app.agents.registry import AgentRegistry
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput
from app.schemas.common import AgentStatus, Exchange, Signal
from app.schemas.market import Candle

END=datetime(2026,11,1,12,tzinfo=timezone.utc)
STEP={"15m":900,"1h":3600,"4h":14400}


def populate(store,*,count=90,tf_set=("15m","1h","4h")):
    for tf in tf_set:
        step=timedelta(seconds=STEP[tf])
        for i in range(count):
            t=END-(count-i-1)*step
            p=100+i*0.2
            store.add(Candle(
                exchange=Exchange.GATE,symbol="BTCUSDT",timeframe=tf,
                open=p,close=p+0.1,high=p+1,low=p-1,volume=50,
                closed_at=t,received_at=t+timedelta(seconds=1),
            ))


def request(*,exchange="GATE",symbol="BTCUSDT",at=END+timedelta(seconds=2)):
    return AgentInput(
        correlation_id="gate-s1-shadow",
        agent_name="GateProfessionalChartShadowAgent",
        timestamp=at,symbol=symbol,timeframe="15m",
        market_context={"exchange":exchange},
    )


def test_gate_chart_agent_is_only_shadow_and_emits_evidence_not_buy_sell():
    store=CandleStore()
    populate(store)
    agent=GateProfessionalChartShadowAgent(store)
    registry=AgentRegistry([agent])
    assert registry.decision_agents()==[]
    assert registry.shadow_agents()==[agent]
    assert agent.registration().decision_role=="SHADOW"
    output=asyncio.run(agent.run(request()))
    assert output.status==AgentStatus.COMPLETED
    assert output.signal==Signal.NEUTRAL
    assert output.confidence==0
    assert len(output.evidence["chart_readings"])==3
    assert output.evidence["venue"]=="GATE_FUTURES_TESTNET_RESEARCH_ONLY"
    assert not output.evidence["execution_authorized"]
    assert not output.evidence["edge_proven"]
    assert not output.evidence["source_authenticated"]


def test_insufficient_market_data_must_wait():
    store=CandleStore()
    populate(store,tf_set=("15m","1h"))
    output=asyncio.run(GateProfessionalChartShadowAgent(store).run(request()))
    assert output.signal==Signal.WAIT
    assert "INSUFFICIENT_HISTORY" in output.warnings


def test_non_gate_or_unapproved_contract_must_block():
    store=CandleStore()
    populate(store)
    agent=GateProfessionalChartShadowAgent(store)
    for input_ in (request(exchange="BINANCE"),request(symbol="UNKNOWN_USDT")):
        output=asyncio.run(agent.run(input_))
        assert output.status==AgentStatus.BLOCKED
        assert output.signal==Signal.WAIT
        assert output.evidence=={}


def test_stale_series_blocks_and_is_not_a_trade_recommendation():
    store=CandleStore()
    populate(store)
    output=asyncio.run(GateProfessionalChartShadowAgent(store).run(
        request(at=END+timedelta(hours=12))
    ))
    assert output.signal==Signal.WAIT
    assert output.confidence==0
    assert "CAUSAL_DATA_QUALITY_BLOCKED" in output.warnings
    assert not output.evidence["execution_authorized"]

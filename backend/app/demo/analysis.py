"""Existing 300-agent cohort and central-risk evaluation in a credential-free subprocess."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import sys
from datetime import datetime, timezone
from uuid import uuid4

from app.agents import indicators
from app.api.context import build_context
from app.core.config import Settings
from app.core.state_machine import SystemState
from app.orchestrator.decision_engine import DecisionEngine
from app.schemas.agents import AgentInput, AgentOutput
from app.schemas.common import AgentStatus, Signal, Exchange, CandidateAction
from app.schemas.decisions import Decision
from app.schemas.market import Candle


async def analyze(payload):
    settings = Settings(_env_file=None, APP_ENV='test', DATABASE_URL='', REDIS_URL=None,
        SYSTEM_MODE='PAPER', OMS_EXECUTION_ENVIRONMENT='PAPER', OMS_TESTNET_ENABLED=False,
        OFFICE_ASYNC_ENABLED=False, RISK_PER_TRADE_PERCENT=.1, MAX_OPEN_POSITIONS=1,
        DEFAULT_LEVERAGE=1, MAX_LEVERAGE_SIMULATED=1, PAPER_INITIAL_BALANCE=payload['equity'])
    ctx=build_context(settings, with_database=False, external_evidence_enabled=False)
    await ctx.state_machine.transition(SystemState.INITIALIZING,reason='Demo isolated analysis',actor='demo')
    await ctx.state_machine.transition(SystemState.PAPER,reason='Virtual account only',actor='demo')
    rows=payload['candles']; symbol=payload['symbol']; timeframe=payload['timeframe']
    exchange=payload.get('exchange', 'BINANCE')
    if exchange not in {'BINANCE', 'GATE'}:
        raise ValueError('unsupported demo market provenance')
    market='GATE_USDT_FUTURES_TESTNET' if exchange=='GATE' else 'USD_M_FUTURES_DEMO'
    candles=[Candle(exchange=Exchange(exchange),symbol=symbol,timeframe=timeframe,
        open=r['open'],high=r['high'],low=r['low'],close=r['close'],volume=r['volume'],
        closed_at=datetime.fromtimestamp(r['close_time_ms']/1000,timezone.utc)) for r in rows]
    for candle in candles:
        ctx.candle_store.add(candle)
    ctx.market_connected=True
    stamp=candles[-1].closed_at; correlation=str(uuid4())
    outputs=[]
    semaphore=asyncio.Semaphore(8)
    async def run(agent):
        async with semaphore:
            # External-evidence specialists remain explicit abstentions rather than
            # reading unrelated Spot evidence or fabricating unavailable inputs.
            if hasattr(agent,'_evidence_service') or getattr(agent,'_derivatives_collector',None):
                return AgentOutput(agent_name=agent.name,status=AgentStatus.SKIPPED,signal=Signal.WAIT,
                    confidence=0,reason='Evidence source not available in this isolated Futures snapshot',warnings=['DEMO_EXTERNAL_EVIDENCE_UNAVAILABLE'])
            agent.timeout_ms=1000
            return await agent.run(AgentInput(agent_name=agent.name,correlation_id=correlation,
                timestamp=stamp,symbol=symbol,timeframe=timeframe,market_context={'exchange':exchange,'market':market}))
    outputs=await asyncio.gather(*(run(a) for a in ctx.agent_registry.agents.values()))
    decision=DecisionEngine().consolidate(correlation_id=correlation,symbol=symbol,timeframe=timeframe,agent_outputs=outputs)
    source='AGENTS'
    if payload.get('canary_side') in {'BUY','SELL'}:
        source='OPERATIONAL_CANARY'
        decision=Decision(correlation_id=correlation,symbol=symbol,timeframe=timeframe,
            candidate_action=CandidateAction(payload['canary_side']),confidence=100,strategy='DEMO_OPERATIONAL_CANARY',reason='Virtual-funds operational test')
    price=payload['price']
    atr=price*.01 if source=='OPERATIONAL_CANARY' else indicators.atr(candles)
    risk=None
    if decision.candidate_action in {CandidateAction.BUY,CandidateAction.SELL}:
        check=await ctx.risk_manager.check(decision,entry_price=price,atr=atr,balance=payload['equity'],
            leverage=1,max_notional_override=50 if source=='OPERATIONAL_CANARY' else 200,
            idempotency_key=f"demo:{symbol}:{timeframe}:{stamp.isoformat()}:{source}:{decision.candidate_action.value}")
        risk=check.model_dump(mode='json')
    return {'id':decision.decision_id,'created_at':datetime.now(timezone.utc).isoformat(),
        'source':source,'exchange':exchange,'market':market,'symbol':symbol,'timeframe':timeframe,'candle_closed_at':stamp.isoformat(),
        'reference_price':price,'decision':decision.model_dump(mode='json'),'risk':risk,
        'agents':[o.model_dump(mode='json') for o in outputs], 'total_agents':len(outputs),
        'primary_agents':['MarketDataAgent','QuantAgent','TrendAgent'],'shadow_decision_authority':False}


def main():
    logging.disable(logging.CRITICAL)
    payload=json.loads(sys.stdin.read())
    # No noisy agent logs enter the machine-readable boundary.
    with contextlib.redirect_stdout(io.StringIO()):
        result=asyncio.run(analyze(payload))
    print(json.dumps(result,allow_nan=False))


if __name__=='__main__':
    main()

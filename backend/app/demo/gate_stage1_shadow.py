"""Stage S1 — isolated Gate Futures TestNet professional market SHADOW bridge.

Uses the EXISTING public GET-only Gate candle adapter, preserving existing
symbol names and quanto_multiplier conversion. It NEVER calls OMS, risk,
position services, key/secret access or the 300-agent decision consolidator.
Receipts mark when an HTTP historical snapshot is returned; historical bars
are not misrepresented as prospectively received at their original close.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Awaitable

from app.agents.gate_professional_chart_shadow import GateProfessionalChartShadowAgent
from app.demo.gate_market import fetch_candles
from app.market_data.store import CandleStore
from app.schemas.market import Candle
from app.schemas.common import Exchange, Signal, AgentStatus
from app.schemas.agents import AgentInput

GATE_S1_SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT")
GATE_S1_TIMEFRAMES=("15m","1h","4h")
GATE_S1_HISTORY=200


async def professional_shadow_snapshot(
    symbol: str, *,
    fetch: Callable[...,Awaitable[list[dict]]]=fetch_candles,
    observation_clock: Callable[[],datetime]=lambda: datetime.now(timezone.utc),
) -> dict:
    """Read all 3 Gate timeframes, evaluate an actual opt-in SHADOW agent."""
    if symbol not in GATE_S1_SYMBOLS:
        raise ValueError("Unapproved Gate contract")
    store=CandleStore()
    receipts={}
    for tf in GATE_S1_TIMEFRAMES:
        rows=await fetch(symbol,tf,limit=GATE_S1_HISTORY)
        observed=observation_clock()
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("Clock must be timezone aware")
        observed=observed.astimezone(timezone.utc)
        receipts[tf]=observed
        if not isinstance(rows,list) or len(rows)!=GATE_S1_HISTORY:
            raise ValueError("Gate Stage 1 requires 200 complete OHLCV candles")
        for row in rows:
            if not isinstance(row,dict):
                raise ValueError("Malformed Gate OHLCV snapshot")
            candle=Candle(
                exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
                open=row["open"],high=row["high"],low=row["low"],
                close=row["close"],volume=row["volume"],
                closed_at=datetime.fromtimestamp(row["close_time_ms"]/1000,timezone.utc),
                received_at=observed,
            )
            # Candidate charts are intentionally not persisted to the
            # existing OMS, volume, audit or 300-agent in-memory stores.
            if not store.add(candle):
                raise ValueError("Duplicate Gate OHLCV")
    as_of=observation_clock()
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Clock must be timezone aware")
    as_of=as_of.astimezone(timezone.utc)
    if as_of<max(receipts.values()):
        raise ValueError("Non-monotonic observation clock")
    agent=GateProfessionalChartShadowAgent(store)
    await agent.initialize()
    output=await agent.run(AgentInput(
        agent_name=agent.name,correlation_id="gate-stage1-observation",
        timestamp=as_of,symbol=symbol,timeframe="15m",
        market_context={"exchange":"GATE","source":"GATE_FUTURES_TESTNET_PUBLIC_READ_ONLY"},
    ))
    if output.signal not in {Signal.NEUTRAL,Signal.WAIT}:
        raise ValueError("Stage 1 SHADOW produced executable signal")
    if output.status not in {AgentStatus.COMPLETED,AgentStatus.BLOCKED}:
        raise ValueError("Stage 1 SHADOW failed")
    if output.evidence.get("execution_authorized") is not False:
        raise ValueError("Stage 1 SHADOW missing explicit execution denial")
    if output.status==AgentStatus.COMPLETED and output.signal==Signal.NEUTRAL:
        stage_status="READ_ONLY_CHART_READY"
    else:
        stage_status="BLOCKED_INSUFFICIENT_OR_UNAVAILABLE_DATA"
    return {
        "stage":"PRO_TRADER_S1",
        "mode":"GATE_TESTNET_PUBLIC_MARKET_SHADOW_ONLY",
        "symbol":symbol,
        "as_of_utc":as_of.isoformat(),
        "receipt_clocks_utc":{k:v.isoformat() for k,v in receipts.items()},
        "status":stage_status,
        "agent_name":agent.name,
        "agent_version":agent.version,
        "agent_signal":output.signal.value,
        "candles_each":GATE_S1_HISTORY,
        "chart_evidence":output.evidence,
        "historical_snapshots_are_prospective_proof":False,
        "source_exchange_authenticated":False,
        "execution_authorized":False,
        "source_decision_cohort_modified":False,
    }

"""Gate S1 integration: real agent contract on existing public candle adapter."""
from datetime import datetime, timedelta, timezone
import asyncio

import pytest

from app.demo.gate_stage1_shadow import professional_shadow_snapshot, GATE_S1_HISTORY


def _rows(tf, *, size=GATE_S1_HISTORY, now=None):
    now=now or datetime.now(timezone.utc)
    step={"15m":900,"1h":3600,"4h":14400}[tf]
    last_close=(int(now.timestamp())//step)*step
    return [{
        "close_time_ms":(last_close-(size-i-1)*step)*1000,
        "open":100+i*.15, "high":102+i*.15,
        "low":99+i*.15,"close":100.25+i*.15,
        "volume":.5,
    } for i in range(size)]


@pytest.mark.asyncio
async def test_gate_s1_actual_shadow_agent_uses_three_existing_market_series():
    seen=[]
    now=datetime.now(timezone.utc)
    async def market_fetch(symbol,tf,*,limit):
        seen.append((symbol,tf,limit))
        return _rows(tf,now=now)
    result=await professional_shadow_snapshot(
        "BTCUSDT",fetch=market_fetch,
        observation_clock=lambda:now+timedelta(seconds=2),
    )
    assert seen==[("BTCUSDT",tf,200) for tf in ("15m","1h","4h")]
    assert result["status"]=="READ_ONLY_CHART_READY"
    assert result["agent_name"]=="GateProfessionalChartShadowAgent"
    assert result["agent_signal"]=="NEUTRAL"
    assert result["chart_evidence"]["timeframe_alignment"]=="BULLISH_ALIGNMENT"
    assert len(result["chart_evidence"]["chart_readings"])==3
    assert not result["execution_authorized"]
    assert not result["source_decision_cohort_modified"]
    assert not result["historical_snapshots_are_prospective_proof"]
    assert len(result["receipt_clocks_utc"])==3


@pytest.mark.asyncio
async def test_prohibit_unapproved_symbol_before_any_market_call():
    async def forbidden(*args,**kwargs):
        raise AssertionError("Unapproved market was fetched")
    with pytest.raises(ValueError,match="Unapproved"):
        await professional_shadow_snapshot("DOGEUSDT",fetch=forbidden)


@pytest.mark.asyncio
async def test_gate_s1_history_gap_is_a_wait_not_false_trend():
    now=datetime.now(timezone.utc)
    async def market_fetch(symbol,tf,*,limit):
        bars=_rows(tf,now=now)
        if tf=="1h":
            return bars[:95]+bars[96:]
        return bars
    with pytest.raises(ValueError,match="200 complete"):
        await professional_shadow_snapshot("ETHUSDT",fetch=market_fetch)


@pytest.mark.asyncio
async def test_gate_s1_receipt_is_observed_after_fetch_not_historical_close():
    now=datetime.now(timezone.utc)
    steps=iter([
        now+timedelta(seconds=2),
        now+timedelta(seconds=3),
        now+timedelta(seconds=4),
        now+timedelta(seconds=5),
    ])
    async def market_fetch(symbol,tf,*,limit):
        return _rows(tf,now=now)
    result=await professional_shadow_snapshot(
        "SOLUSDT",fetch=market_fetch,observation_clock=lambda:next(steps)
    )
    assert result["receipt_clocks_utc"]["4h"]==(now+timedelta(seconds=4)).isoformat()
    assert result["as_of_utc"]==(now+timedelta(seconds=5)).isoformat()


@pytest.mark.asyncio
async def test_clock_rollback_and_unreceived_candles_block_without_trade():
    now=datetime.now(timezone.utc)
    steps=iter([now+timedelta(seconds=10),
                now+timedelta(seconds=9),
                now+timedelta(seconds=8),
                now])
    async def market_fetch(symbol,tf,*,limit):
        return _rows(tf,now=now)
    with pytest.raises(ValueError,match="Non-monotonic"):
        await professional_shadow_snapshot(
            "BTCUSDT",fetch=market_fetch,observation_clock=lambda:next(steps)
        )


@pytest.mark.asyncio
async def test_invalid_market_rows_are_not_silently_repaired():
    now=datetime.now(timezone.utc)
    async def market_fetch(symbol,tf,*,limit):
        data=_rows(tf,now=now)
        if tf=="15m":
            data[0]["high"] = 0
        return data
    with pytest.raises(ValueError,match="Invalid candle"):
        await professional_shadow_snapshot("BTCUSDT",fetch=market_fetch)

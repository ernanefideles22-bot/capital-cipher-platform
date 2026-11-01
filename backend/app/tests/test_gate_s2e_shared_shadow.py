"""Stage S2E uses one S1 market snapshot and never grants broker authority."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.demo.gate_stage1_shadow import professional_shadow_snapshot
from app.demo.gate_service import create_app
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_s2e_live_shadow import evaluate_shared_shadow

UTC=timezone.utc
NOW=datetime(2026,11,1,13,30,tzinfo=UTC)
STEPS={"15m":900,"1h":3600,"4h":14400}


def market_bars(tf,n=200,symbol="BTCUSDT",*,received=None):
    step=STEPS[tf]
    end=(int(NOW.timestamp())//step)*step
    bars=[]
    for j in range(n):
        close=datetime.fromtimestamp(end-(n-1-j)*step,UTC)
        price=100+j*.15
        bars.append(Candle(exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
                           open=price,high=price+1.5,low=price-1,
                           close=price+.25,volume=100,
                           closed_at=close,
                           received_at=received or NOW+timedelta(seconds=2)))
    return bars


def same_observation(tf):
    return [
        {"close_time_ms":int(b.closed_at.timestamp()*1000),
         "open":b.open,"high":b.high,"low":b.low,
         "close":b.close,"volume":b.volume}
        for b in market_bars(tf)
    ]


def test_real_s1_s2_shared_candle_confluence_no_trade_or_secret():
    groups={tf:market_bars(tf) for tf in STEPS}
    report=evaluate_shared_shadow(
        "BTCUSDT",groups,as_of=NOW+timedelta(seconds=3))
    assert report["status"]=="READ_ONLY_CONFLUENCE_READY"
    assert report["s1_frames"]==["4h","1h","15m"]
    assert report["candidate_count"]+report["filtered_count"]<=3
    assert len(report["hypotheses"])==3
    assert not report["oms_touched"]
    assert not report["source_exchange_authenticated"]
    assert not report["execution_authorized"]
    assert not report["predictive_edge_proven"]
    assert all(v["execution_authorized"] is False for v in report["hypotheses"])


def test_live_s2e_does_not_rewrite_historical_receipt_times():
    groups={tf:market_bars(tf) for tf in STEPS}
    old=groups["1h"][0].received_at
    _=evaluate_shared_shadow(
        "BTCUSDT",groups,as_of=NOW+timedelta(seconds=4))
    assert groups["1h"][0].received_at==old
    groups["1h"][-1]=groups["1h"][-1].model_copy(
        update={"received_at":NOW+timedelta(hours=2)})
    with pytest.raises(ValueError,match="not-yet-received|Future"):
        evaluate_shared_shadow(
            "BTCUSDT",groups,as_of=NOW+timedelta(seconds=4))


@pytest.mark.asyncio
async def test_shared_snapshot_fetches_only_original_nine_timeseries():
    seen=[]
    async def fetch(symbol,tf,*,limit):
        seen.append((symbol,tf,limit))
        assert symbol=="BTCUSDT"
        return same_observation(tf)
    result=await professional_shadow_snapshot(
        "BTCUSDT",fetch=fetch,
        observation_clock=lambda:NOW+timedelta(seconds=3),
        strategy_shadow_hook=evaluate_shared_shadow)
    assert seen==[("BTCUSDT",tf,200) for tf in ("15m","1h","4h")]
    assert result["status"]=="READ_ONLY_CHART_READY"
    assert result["strategy_shadow"]["status"]=="READ_ONLY_CONFLUENCE_READY"
    assert result["agent_signal"]=="NEUTRAL"
    assert not result["execution_authorized"]
    assert not result["strategy_shadow"]["execution_authorized"]


@pytest.mark.asyncio
async def test_s2e_extension_failure_does_not_corrupt_original_s1():
    async def fetch(symbol,tf,*,limit):
        return same_observation(tf)
    def bad_runner(*args,**kwargs):
        raise RuntimeError("SECRET-NEVER-LEAK")
    result=await professional_shadow_snapshot(
        "BTCUSDT",fetch=fetch,
        observation_clock=lambda:NOW+timedelta(seconds=3),
        strategy_shadow_hook=bad_runner)
    assert result["status"]=="READ_ONLY_CHART_READY"
    assert result["strategy_shadow"]["status"]=="BLOCKED"
    assert "SECRET-NEVER-LEAK" not in str(result)
    assert not result["strategy_shadow"]["execution_authorized"]
    def executable(*args,**kwargs):
        return {"symbol":"BTCUSDT","status":"READ_ONLY_CONFLUENCE_READY",
                "execution_authorized":True,
                "oms_touched":False,"source_decision_cohort_modified":False}
    refused=await professional_shadow_snapshot(
        "BTCUSDT",fetch=fetch,
        observation_clock=lambda:NOW+timedelta(seconds=3),
        strategy_shadow_hook=executable)
    assert refused["status"]=="READ_ONLY_CHART_READY"
    assert refused["strategy_shadow"]["status"]=="BLOCKED"


async def fake_market(*a,**k):
    return [{}]*60


def test_setting_s2e_without_s1_keeps_everything_disabled():
    calls=[]
    async def run(symbol,**kw):
        calls.append(symbol)
        raise AssertionError("S1 flag not enabled")
    app=create_app(environment={"GATE_S2E_SHADOW_ENABLED":"true"},
                   stage1_shadow_runner=run,market_fetch=fake_market)
    with TestClient(app) as client:
        status=client.get("/api/gate/status").json()
        assert status["professional_shadow"]["status"]=="DISABLED"
        assert status["strategy_shadow"]["status"]=="DISABLED"
        assert not status["execution_enabled"]
    assert not calls


def test_s2e_optin_same_stage1_cycle_and_secret_redacted():
    calls=[]
    def passive_extension(symbol,by_timeframe,*,as_of):
        assert not hasattr(by_timeframe,"key")
        return {"symbol":symbol,"status":"READ_ONLY_CONFLUENCE_READY",
                "oms_touched":False,
                "source_decision_cohort_modified":False,
                "execution_authorized":False}
    async def shadow(symbol,*,strategy_shadow_hook):
        calls.append(symbol)
        return {"symbol":symbol,"status":"READ_ONLY_CHART_READY",
                "agent_signal":"NEUTRAL","execution_authorized":False,
                "source_decision_cohort_modified":False,
                "strategy_shadow":{
                    "symbol":symbol,"status":"READ_ONLY_CONFLUENCE_READY",
                    "execution_authorized":False,
                    "source_decision_cohort_modified":False,
                    "oms_touched":False,
                    "candidate_count":0,"filtered_count":0,
                }}
    secret="PRIVATE-TESTNET-SECRET-NOT-FOR-LOGS"
    app=create_app(environment={
        "GATE_S1_SHADOW_ENABLED":"true",
        "GATE_S2E_SHADOW_ENABLED":"true",
        "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET":secret,
    },stage1_shadow_runner=shadow,strategy_shadow_runner=passive_extension,
        market_fetch=fake_market)
    with TestClient(app) as client:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=client.get("/api/gate/status").json()
            if status["strategy_shadow"]["checked_at"]:
                break
            time.sleep(.015)
        assert status["professional_shadow"]["status"]=="AVAILABLE"
        assert status["strategy_shadow"]["status"]=="AVAILABLE"
        assert status["strategy_shadow"]["execution_authorized"] is False
        assert not status["execution_enabled"]
        assert "PRIVATE-TESTNET-SECRET" not in str(status)
        assert client.post("/api/demo/execute").status_code==404
    assert calls==["BTCUSDT","ETHUSDT","SOLUSDT"]


def test_s2e_partial_failure_isolated_from_s1_status():
    async def shadow(symbol,*,strategy_shadow_hook):
        return {
            "status":"READ_ONLY_CHART_READY","symbol":symbol,
            "agent_signal":"NEUTRAL","execution_authorized":False,
            "source_decision_cohort_modified":False,
            "strategy_shadow":{
                "status":"BLOCKED" if symbol=="ETHUSDT"
                else "READ_ONLY_CONFLUENCE_READY",
                "execution_authorized":False,"oms_touched":False,
                "source_decision_cohort_modified":False,
                "candidate_count":0,"filtered_count":0,
            },
        }
    app=create_app(
        environment={"GATE_S1_SHADOW_ENABLED":"true",
                     "GATE_S2E_SHADOW_ENABLED":"true"},
        stage1_shadow_runner=shadow,market_fetch=fake_market)
    with TestClient(app) as client:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            status=client.get("/api/gate/status").json()
            if status["strategy_shadow"]["checked_at"]:
                break
            time.sleep(.015)
        assert status["professional_shadow"]["status"]=="AVAILABLE"
        assert status["strategy_shadow"]["status"]=="PARTIAL_OR_BLOCKED"
        assert status["strategy_shadow"]["markets"]["ETHUSDT"]["status"]=="BLOCKED"
        assert status["execution_enabled"] is False

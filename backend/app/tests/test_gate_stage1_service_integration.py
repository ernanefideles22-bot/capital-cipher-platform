"""Gate S1 service opt-in smoke: market-only diagnostic must not touch OMS."""
import time
import asyncio
from fastapi.testclient import TestClient

from app.demo.gate_service import create_app


async def fake_market(*args,**kwargs):
    return [{}]*60


async def fake_inventory(*args,**kwargs):
    return {"positions":0,"orders":0,"conditional_orders":0,
            "atomic":False,"execution_authorized":False}


async def fake_position(*args,**kwargs):
    return {"mode":"single","margin":"isolated","leverage":"1",
            "one_way_isolated_1x":True,"execution_authorized":False}


def _status(client):
    for _ in range(200):
        data=client.get("/api/gate/status").json()
        if data["professional_shadow"]["checked_at"] is not None:
            return data
        time.sleep(.01)
    raise AssertionError("Gate stage1 shadow did not emit status")


def test_legacy_gate_default_has_no_stage1_requests():
    calls=[]
    async def unused(symbol):
        calls.append(symbol)
        raise AssertionError("No background collector by default")
    app=create_app(
        environment={},market_fetch=fake_market,inventory_fn=fake_inventory,
        position_config_fn=fake_position,stage1_shadow_runner=unused,
    )
    with TestClient(app) as client:
        data=client.get("/api/gate/status").json()
        assert data["professional_shadow"]["status"]=="DISABLED"
        assert data["execution_enabled"] is False
    assert not calls


def test_optin_stage1_evaluates_all_three_without_reading_credentials():
    calls=[]
    async def run(symbol):
        calls.append(symbol)
        return {"symbol":symbol,"status":"READ_ONLY_CHART_READY",
                "agent_signal":"NEUTRAL",
                "source_decision_cohort_modified":False,
                "execution_authorized":False,
                "chart_evidence":{"timeframe_alignment":"BULLISH_ALIGNMENT"}}
    app=create_app(
        environment={"GATE_S1_SHADOW_ENABLED":"true",
                     "CAPITAL_CIPHER_GATE_TESTNET_API_SECRET":"should-not-appear"},
        market_fetch=fake_market,inventory_fn=fake_inventory,
        position_config_fn=fake_position,stage1_shadow_runner=run,
    )
    with TestClient(app) as client:
        data=_status(client)
        assert data["professional_shadow"]["status"]=="AVAILABLE"
        assert set(data["professional_shadow"]["markets"]) == {
            "BTCUSDT","ETHUSDT","SOLUSDT"
        }
        assert data["professional_shadow"]["execution_authorized"] is False
        assert data["execution_enabled"] is False
        assert "should-not-appear" not in str(data)
        assert client.post("/api/demo/execute").status_code==404
    assert calls == ["BTCUSDT","ETHUSDT","SOLUSDT"]


def test_partial_failure_remains_readonly_and_sanitizes_private_errors():
    async def run(symbol):
        if symbol=="ETHUSDT":
            raise RuntimeError("DO NOT DISPLAY PRIVATE SECRET")
        return {"symbol":symbol,"status":"READ_ONLY_CHART_READY",
                "agent_signal":"NEUTRAL",
                "source_decision_cohort_modified":False,
                "execution_authorized":False}
    app=create_app(
        environment={"GATE_S1_SHADOW_ENABLED":"true"},
        market_fetch=fake_market,inventory_fn=fake_inventory,
        position_config_fn=fake_position,stage1_shadow_runner=run,
    )
    with TestClient(app) as client:
        data=_status(client)
        assert data["professional_shadow"]["status"]=="PARTIAL_OR_BLOCKED"
        assert data["professional_shadow"]["markets"]["ETHUSDT"]["error_type"]=="RuntimeError"
        assert "PRIVATE SECRET" not in str(data)
        assert not data["execution_enabled"]


def test_shadow_multiple_cycles_streak_latency_and_bounded_history():
    calls=[]
    async def run(symbol):
        calls.append(symbol)
        return {"symbol":symbol,"status":"READ_ONLY_CHART_READY",
                "agent_signal":"NEUTRAL",
                "source_decision_cohort_modified":False,
                "execution_authorized":False,
                "total_duration_ms":12.0,
                "chart_evidence":{"timeframe_alignment":"BULLISH_ALIGNMENT"}}
    async def fast_test_only_pause(_seconds):
        await asyncio.sleep(.012)
    app=create_app(
        environment={"GATE_S1_SHADOW_ENABLED":"true"},
        market_fetch=fake_market,inventory_fn=fake_inventory,
        position_config_fn=fake_position,stage1_shadow_runner=run,
        stage1_sleep=fast_test_only_pause,
    )
    with TestClient(app) as client:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            info=client.get("/api/gate/status").json()["professional_shadow"]
            if info["cycle_count"]>=18:
                break
            time.sleep(.01)
        assert info["cycle_count"]>=18
        assert info["consecutive_ready_cycles"]>=18
        assert len(info["recent_cycles"])==16
        assert info["latency_samples"]==16
        assert info["cycle_duration_p50_ms"]>=0
        assert info["cycle_duration_p95_ms"]>=info["cycle_duration_p50_ms"]
        assert info["cycle_duration_p99_ms"]>=info["cycle_duration_p95_ms"]
        assert all(x["status"]=="AVAILABLE" for x in info["recent_cycles"])
        assert not info["execution_authorized"]
        assert not client.get("/api/gate/status").json()["execution_enabled"]
        assert client.post("/api/demo/execute").status_code==404
    assert len(calls)>=18*3


def test_shadow_failure_resets_ready_streak_without_affecting_oms():
    counter=0
    async def run(symbol):
        nonlocal counter
        if symbol=="BTCUSDT":
            counter+=1
        if symbol=="ETHUSDT" and counter==2:
            raise RuntimeError("secret data must stay private")
        return {"symbol":symbol,"status":"READ_ONLY_CHART_READY",
                "agent_signal":"NEUTRAL",
                "source_decision_cohort_modified":False,
                "execution_authorized":False}
    async def fast_test_only_pause(_seconds):
        await asyncio.sleep(.025)
    app=create_app(
        environment={"GATE_S1_SHADOW_ENABLED":"true"},
        market_fetch=fake_market,inventory_fn=fake_inventory,
        position_config_fn=fake_position,
        stage1_shadow_runner=run,stage1_sleep=fast_test_only_pause,
    )
    with TestClient(app) as client:
        deadline=time.monotonic()+3
        history=[]
        while time.monotonic()<deadline:
            info=client.get("/api/gate/status").json()["professional_shadow"]
            history=info.get("recent_cycles",[])
            if len(history)>=4:
                break
            time.sleep(.005)
        assert len(history)>=4
        assert history[0]["status"]=="AVAILABLE"
        assert history[1]["status"]=="PARTIAL_OR_BLOCKED"
        assert history[2]["status"]=="AVAILABLE"
        assert info["consecutive_ready_cycles"]==info["cycle_count"]-2
        assert "secret data must stay private" not in str(info)
        assert not client.get("/api/gate/status").json()["execution_enabled"]

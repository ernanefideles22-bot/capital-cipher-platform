"""Gate S1 service opt-in smoke: market-only diagnostic must not touch OMS."""
import time
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

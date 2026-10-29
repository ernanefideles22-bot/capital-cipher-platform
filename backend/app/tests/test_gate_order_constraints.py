import pytest
from app.execution.adapters.gate_order_constraints import (
    GateConstraintError, gate_client_id, prepare_contract_quantity,
)


def inputs():
    return {"symbol":"BTCUSDT", "side":"BUY", "requested_quantity":"0.00025",
            "max_notional":"20", "reference_price":"80000", "observed_at":1000, "now":1005,
            "metadata":{"name":"BTC_USDT","quanto_multiplier":"0.0001",
                        "order_size_min":"1","order_size_max":"1000","in_delisting":False}}


def test_floor_contracts_without_exceeding_asset_or_approval():
    result=prepare_contract_quantity(**inputs())
    assert result["signed_contracts"] == 2
    assert result["base_quantity"] == "0.0002"
    assert result["reference_notional"] == "16.0000"
    assert result["execution_authorized"] is False
    data=inputs()
    data.update(side="SELL",max_notional="8")
    assert prepare_contract_quantity(**data)["signed_contracts"] == -1


@pytest.mark.parametrize("section,key,value", [
    ("metadata","name","ETH_USDT"), ("metadata","in_delisting",True),
    ("metadata","in_delisting",None), ("metadata","quanto_multiplier","NaN"),
    ("metadata","quanto_multiplier",0), ("metadata","order_size_min","3"),
    ("metadata","order_size_max",0), ("metadata","order_size_max","0.5"),
    (None,"requested_quantity","0.00001"), (None,"max_notional","1"),
    (None,"reference_price",float("nan")), (None,"side","LONG"),
    (None,"symbol","DOGEUSDT"), (None,"observed_at",990), (None,"observed_at",1010),
])
def test_invalid_or_stale_metadata_is_rejected(section,key,value):
    data=inputs()
    (data[section] if section else data)[key]=value
    with pytest.raises(GateConstraintError):
        prepare_contract_quantity(**data)


def test_venue_client_id_is_deterministic_and_restricted():
    first=gate_client_id("a"*64)
    assert first == gate_client_id("a"*64)
    assert first != gate_client_id("b"*64)
    assert first.startswith("t-") and len(first)==30
    with pytest.raises(GateConstraintError): gate_client_id("invalid")

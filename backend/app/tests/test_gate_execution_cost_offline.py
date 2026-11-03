import copy
import json
import pytest
from app.strategy import gate_execution_cost_offline as s
from app.tests.test_gate_execution_cost_audit import metas,receipt


def test_complete_metadata_preserves_original_quote_arithmetic():
    m=metas();p=s.partial_metadata(m["spot"],m["perp"],"BTC_USDT")
    assert p["unverified_size_limits"]==[]
    a=s.original.analyze_pair(m,receipt(),receipt("perp"))
    b=s.original.analyze_pair(p,receipt(),receipt("perp"))
    assert a==b


def test_zero_minimum_does_not_invent_positive_size_certainty():
    m=metas();m["perp"]["order_size_min"]=0
    p=s.partial_metadata(m["spot"],m["perp"],"BTC_USDT")
    assert p["unverified_size_limits"]==["perp.effective_minimum_positive_size"]
    assert p["whole_positive_contracts_only"] and p["source_contract_minimum"]=="0"
    result=s.original.analyze_pair(p,receipt(),receipt("perp"))
    assert any(q["status"]=="STATIC_QUOTE_AVAILABLE" for q in result["quotes"])


def test_absent_spot_limit_is_unknown_and_input_not_mutated():
    m=metas();del m["spot"]["max_base_amount"];before=copy.deepcopy(m)
    p=s.partial_metadata(m["spot"],m["perp"],"BTC_USDT")
    assert p["unverified_size_limits"]==["spot.max_base_amount"]
    assert m==before and "max_base_amount" not in m["spot"]


@pytest.mark.parametrize("kind", ["negative_min","inverse","delisting"])
def test_invalid_contract_never_rescued_by_partial_adapter(kind):
    m=metas()
    if kind=="negative_min":m["perp"]["order_size_min"]=-1
    elif kind=="inverse":m["perp"]["type"]="inverse"
    else:m["perp"]["in_delisting"]=True
    with pytest.raises(ValueError):s.partial_metadata(m["spot"],m["perp"],"BTC_USDT")


def test_zero_available_levels_are_not_liquidity_and_original_stays_intact():
    r=receipt("perp");raw=json.loads(r["raw_response_utf8"])
    raw["asks"].insert(1,{"p":"101.5","s":"0"})
    r["raw_response_utf8"]=json.dumps(raw);r["response_sha256"]="original";before=copy.deepcopy(r)
    derived,n=s.without_empty_levels(r,"perp")
    assert n==1 and r==before
    assert "response_sha256" not in derived and derived["derived_from_response_sha256"]=="original"
    a=s.original.book(derived,"perp")
    assert len(a["asks"])==2


def test_negative_liquidity_is_never_treated_as_empty_or_filled():
    r=receipt("perp");raw=json.loads(r["raw_response_utf8"]);raw["asks"][0]["s"]="-1"
    r["raw_response_utf8"]=json.dumps(raw);derived,n=s.without_empty_levels(r,"perp")
    assert n==0
    with pytest.raises(ValueError):s.original.book(derived,"perp")


def test_all_empty_book_remains_unavailable():
    r=receipt("perp");raw=json.loads(r["raw_response_utf8"])
    for side in ("asks","bids"):
        for v in raw[side]:v["s"]="0"
    r["raw_response_utf8"]=json.dumps(raw);derived,n=s.without_empty_levels(r,"perp")
    assert n==4
    with pytest.raises(ValueError):s.original.book(derived,"perp")


def test_ignoring_zero_size_does_not_relax_source_clock_checks():
    r=receipt("perp",current=1793740000-100,update=1793740000-100)
    derived,_=s.without_empty_levels(r,"perp")
    with pytest.raises(ValueError):s.original.book(derived,"perp")

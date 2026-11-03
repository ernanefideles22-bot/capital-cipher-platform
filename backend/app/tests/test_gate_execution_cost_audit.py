import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal as Q
from unittest.mock import patch

import pytest

from app.strategy import gate_execution_cost_audit as a

NOW = 1793740000


def metas(mult="0.01", precision=3):
    spot = {"id": "BTC_USDT", "trade_status": "tradable", "amount_precision": precision,
            "min_base_amount": "0.001", "min_quote_amount": "1", "max_base_amount": None,
            "max_quote_amount": None, "fee": "999"}
    perp = {"name": "BTC_USDT", "type": "direct", "in_delisting": False,
            "quanto_multiplier": mult, "order_size_min": 1, "order_size_max": 100000,
            "market_order_size_max": 100000, "taker_fee_rate": "0.0005"}
    return a.metadata(spot, perp, "BTC_USDT")


def receipt(venue="spot", *, current=NOW, update=NOW, ask=101, bid=99):
    levels = {"asks": [(ask, 100), (ask+1, 2000)], "bids": [(bid, 100), (bid-1, 2000)]}
    data = {"id": 1, "current": current * (1000 if venue == "spot" else 1),
            "update": update * (1000 if venue == "spot" else 1)}
    for k, rows in levels.items():
        data[k] = [[str(p), str(q)] for p, q in rows] if venue == "spot" else [{"p":str(p), "s":str(q)} for p,q in rows]
    return {"status": 200, "elapsed_seconds": .1, "received_at_utc": datetime.fromtimestamp(NOW+.5, timezone.utc).isoformat(),
            "raw_response_utf8": json.dumps(data)}


def test_spot_milliseconds_perp_seconds_and_contract_multiplier():
    spot = a.book(receipt(), "spot")
    perp = a.book(receipt("perp"), "perp", Q("0.01"))
    assert spot["current"] == perp["current"] == NOW
    assert spot["asks"][0][1] == 100 and perp["asks"][0][1] == 1


def test_static_vwap_oracle_spread_is_counted_once():
    b = {"asks": [(Q(101),Q(1)),(Q(102),Q(3))], "bids": [(Q(99),Q(1)),(Q(98),Q(3))], "mid":Q(100)}
    q = a.price_quote(b,Q(2))
    assert Q(q["buy_vwap"]) == Q("101.5") and Q(q["sell_vwap"]) == Q("98.5")
    assert q["spread_bps"] == 200
    assert q["buy_cost_from_mid_bps"] == pytest.approx(150)
    assert q["sell_cost_from_mid_bps"] == pytest.approx(150)
    assert q["roundtrip_displayed_price_cost_bps"] == pytest.approx(300)
    assert Q(q["displayed_price_cost_usdt"]) == 6


def test_insufficient_depth_cannot_fabricate_fill():
    with pytest.raises(ValueError, match="INSUFFICIENT"):
        a.vwap([(Q(100),Q(1))],Q(2))


def test_common_lot_is_lcm_not_maximum_or_residual_hedge():
    assert a.common_lot(Q(".02"),Q(".03")) == Q(".06")
    assert a.common_lot(Q(".000001"),Q(".0001")) == Q(".0001")


def test_paired_equal_quantity_four_fee_oracle_and_no_personal_fee_claim():
    m = metas();books={"spot":a.book(receipt(),"spot"),"perp":a.book(receipt("perp"),"perp",Q(".01"))}
    row = a.quote_target(m,books,50,"paired")
    qty = Q(row["quantity"])
    assert qty == Q(".49")
    assert all(Q(q["quantity"]) == qty for q in row["quotes"].values())
    nominal=row["fee_scenarios_not_personal_rates"]["nominal"]
    # Two spot + two perp fees at 20/6 bps, no use of deprecated/public fee fields.
    fees = qty * Q(200) * Q(".0026")
    price_cost = qty * Q(4)
    assert Q(nominal["assumed_fees_usdt"]) == fees
    assert Q(nominal["displayed_price_plus_assumed_fee_usdt"]) == fees+price_cost
    assert not row["personal_total_cost_verified"] and not row["execution_authorized"]


def test_small_budget_fails_whole_futures_lot_but_spot_can_quote():
    m=metas(mult="1");books={"spot":a.book(receipt(),"spot"),"perp":a.book(receipt("perp"),"perp",Q(1))}
    assert a.quote_target(m,books,50,"spot")["status"] == "STATIC_QUOTE_AVAILABLE"
    assert a.quote_target(m,books,50,"perp")["reason"] == "BUDGET_BELOW_AUDITED_LOT"
    assert a.quote_target(m,books,50,"paired")["status"] == "UNAVAILABLE"


def test_spot_minimum_quote_and_market_order_caps():
    m=metas();books={"spot":a.book(receipt(),"spot")}
    m["spot"]["min_quote_amount"]="60"
    assert "min_quote" in a.quote_target(m,books,50,"spot")["reason"]
    m["spot"]["min_quote_amount"]="1";m["spot"]["market_order_max_money"]="20"
    assert "MARKET_SIZE_CAP" in a.quote_target(m,books,50,"spot")["reason"]


@pytest.mark.parametrize("damage", ["old", "future", "crossed", "duplicate", "negative", "unsorted", "nan"])
def test_invalid_book_rejected(damage):
    r=receipt();data=json.loads(r["raw_response_utf8"])
    if damage=="old":data["current"]=(NOW-30)*1000
    elif damage=="future":data["current"]=(NOW+30)*1000
    elif damage=="crossed":data["bids"][0][0]="102"
    elif damage=="duplicate":data["asks"][1][0]=data["asks"][0][0]
    elif damage=="negative":data["asks"][0][1]="-1"
    elif damage=="unsorted":data["asks"].reverse()
    else:data["asks"][0][0]="NaN"
    r["raw_response_utf8"]=json.dumps(data)
    with pytest.raises(ValueError):a.book(r,"spot")


def test_pair_clock_skew_blocks_pair_without_hiding_valid_single_books():
    result=a.analyze_pair(metas(),receipt(current=NOW-7,update=NOW-7),receipt("perp"))
    assert result["pair_generation_skew_seconds"]==7
    assert all(q["status"]=="UNAVAILABLE" for q in result["quotes"] if q["mode"]=="paired")
    assert any(q["status"]=="STATIC_QUOTE_AVAILABLE" for q in result["quotes"] if q["mode"]=="spot")


def test_missing_private_fees_cannot_be_promoted_from_metadata():
    m=metas();assert not m["personal_fee_verified"]
    assert m["public_perp_taker_fee_reference"]=="0.0005"
    assert a.rules()["personal_fee_profile_status"]=="UNVERIFIED_NOT_QUERIED"
    assert not a.rules()["public_contract_fee_is_personal_fee"]


@pytest.mark.parametrize("kind,symbol", [("orders","BTC_USDT"),("wallet/fee","BTC_USDT"),("spot_book","OTHER_USDT")])
def test_public_whitelist_before_network(kind,symbol):
    with patch.object(a,"build_opener",side_effect=AssertionError("network forbidden")):
        with pytest.raises(ValueError):a.public_get(kind,symbol)


def test_register_exclusive_append_only_and_source_frozen(tmp_path):
    a.register(tmp_path)
    with pytest.raises(FileExistsError):a.register(tmp_path)
    with closing(a.base._connect(tmp_path)) as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("DELETE FROM events")
    with patch.object(a,"fingerprint",return_value="changed"):
        with pytest.raises(ValueError):a.review(tmp_path)


def test_unavailable_metadata_is_terminal_no_retry(tmp_path):
    a.register(tmp_path)
    with patch.object(a,"public_get",side_effect=TimeoutError) as get:
        result=a.run(tmp_path)
    assert get.call_count==6 and result["phases"][-1]=="SOURCE_UNAVAILABLE"
    assert len(list((tmp_path/"raw").glob('*.json')))==6
    with pytest.raises(ValueError):a.run(tmp_path)


def test_exact_bounded_capture_budget_and_never_execution(tmp_path):
    a.register(tmp_path)
    def get(kind,symbol):
        if kind.endswith("meta"):
            m=metas();value=copy.deepcopy(m["spot" if kind=="spot_meta" else "perp"])
            value["id" if kind=="spot_meta" else "name"]=symbol
            return {"kind":kind,"symbol":symbol,"status":200,"raw_response_utf8":json.dumps(value)}
        return {**receipt("spot" if kind=="spot_book" else "perp"),"kind":kind,"symbol":symbol}
    with patch.object(a,"public_get",side_effect=get) as calls,patch.object(a.time,"sleep"):
        result=a.run(tmp_path)
    assert calls.call_count==36 and len(result["samples"])==5
    assert result["complete"] and not result["execution_authorized"] and not result["profitability_proven"]
    assert not result["sufficient_to_lower_slippage"] and not result["actual_fill_costs_measured"]
    assert result["personal_fee_profile_status"]=="UNVERIFIED_NOT_QUERIED"

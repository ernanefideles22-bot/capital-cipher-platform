"""E2.6 one-shot forward collector contract tests, with NO network calls."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv

import pytest

from app.strategy.prospective_core_gate_e26 import HEADER
from scripts.capture_e26_public_spot_observations import capture_once


START=datetime(2026,11,5,tzinfo=timezone.utc)
STEPS={"15m":900_000,"1h":3_600_000,"4h":14_400_000}


def fake_binance(symbol, tf, *, from_ms, to_ms, limit):
    step=STEPS[tf]
    assert to_ms==from_ms+limit*step-1
    result=[]
    for i in range(limit):
        opened=from_ms+i*step
        price=100+i/100
        result.append([
            opened,str(price),str(price+1),str(price-1),
            str(price+.3),"100",opened+step-1,
        ])
    return result


def test_bootstrap_writes_300_warmup_plus_first_future_for_all_three(tmp_path):
    now=START+timedelta(seconds=10)
    r=capture_once(tmp_path,fetcher=fake_binance,clock=lambda:now)
    assert r["status"]=="ONE_READ_ONLY_CAPTURE_CYCLE"
    assert len(r["written"])==3
    assert all(x["bars_written"]==301 for x in r["written"])
    assert not r["collection_is_scheduled"]
    assert not r["external_attestation_provided"]
    for symbol,tf in (("BTCUSDT","15m"),("ETHUSDT","1h"),("SOLUSDT","4h")):
        p=tmp_path/f"e26_{symbol}_{tf}_observed.csv"
        with p.open(newline="") as f:
            reader=csv.DictReader(f)
            assert reader.fieldnames==HEADER.split(",")
            rows=list(reader)
        assert len(rows)==301
        assert rows[-1]["observed_at_utc"]==now.isoformat()
        assert rows[-1]["source"]=="BINANCE_PUBLIC_SPOT_KLINES"
    again=capture_once(tmp_path,fetcher=fake_binance,clock=lambda:now)
    assert all(x["status"]=="NEXT_BAR_NOT_CLOSED" for x in again["written"])
    assert all(
        len((tmp_path/f"e26_{s}_{tf}_observed.csv").read_text().splitlines())==302
        for s,tf in (("BTCUSDT","15m"),("ETHUSDT","1h"),("SOLUSDT","4h"))
    )


def test_next_close_append_does_not_fabricate_other_timeframes(tmp_path):
    capture_once(tmp_path,fetcher=fake_binance,
                 clock=lambda:START+timedelta(seconds=10))
    now=START+timedelta(minutes=15,seconds=10)
    r=capture_once(tmp_path,fetcher=fake_binance,clock=lambda:now)
    assert r["written"][0]["bars_written"]==1
    assert r["written"][1]["status"]=="NEXT_BAR_NOT_CLOSED"
    assert r["written"][2]["status"]=="NEXT_BAR_NOT_CLOSED"
    assert len((tmp_path/"e26_BTCUSDT_15m_observed.csv").read_text().splitlines())==303


def test_late_bootstrap_and_missing_candle_are_rejected(tmp_path):
    with pytest.raises(ValueError,match="Missed first forward candle"):
        capture_once(tmp_path,fetcher=fake_binance,
                     clock=lambda:START+timedelta(hours=5))
    capture_once(tmp_path,fetcher=fake_binance,
                 clock=lambda:START+timedelta(seconds=10))
    with pytest.raises(ValueError,match="Missed forward candle"):
        capture_once(tmp_path,fetcher=fake_binance,
                     clock=lambda:START+timedelta(hours=3))


def test_bad_provider_clock_does_not_write_any_csv(tmp_path):
    def shifted(symbol, tf, **kw):
        rows=fake_binance(symbol,tf,**kw)
        rows[0][0]+=10_000
        return rows
    with pytest.raises(ValueError,match="Wrong public candle"):
        capture_once(tmp_path,fetcher=shifted,
                     clock=lambda:START+timedelta(seconds=10))
    assert not list(tmp_path.glob("*.csv"))


def test_prestart_capture_is_inert(tmp_path):
    r=capture_once(tmp_path,fetcher=fake_binance,
                   clock=lambda:datetime(2026,11,1,tzinfo=timezone.utc))
    assert r["status"]=="CAMPAIGN_NOT_STARTED"
    assert list(tmp_path.iterdir())==[]

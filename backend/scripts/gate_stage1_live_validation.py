"""One-shot Gate Futures TestNet live, public, READ-ONLY independent S1 audit.

No credentials, database, OMS, trading endpoint, deployment or scheduling.
Fetch normalized Gate source OHLCV and recompute morphology, confirmed pivots,
support/resistance and Fibonacci *independently* of the S1 chart engine.
This is a shape correctness audit, NOT a profitability or prediction test.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime, timezone

from app.demo.gate_market import fetch_candles
from app.demo.gate_stage1_shadow import professional_shadow_snapshot

PERIOD={"15m":900,"1h":3600,"4h":14400}
MARKETS=("BTCUSDT","ETHUSDT","SOLUSDT")


def independent_patterns(bars):
    prev, last=bars[-2:]
    op, hi, lo, cl=last["open"],last["high"],last["low"],last["close"]
    span=hi-lo
    if span==0:
        return {"ZERO_RANGE"}
    body=abs(cl-op)
    lower=min(op,cl)-lo
    upper=hi-max(op,cl)
    out=set()
    if body/span<=.1:out.add("DOJI")
    if body/span<=.4 and lower>=.6*span and lower>=2*body and upper<=body:
        out.add("HAMMER_SHAPE")
    if body/span<=.4 and upper>=.6*span and upper>=2*body and lower<=body:
        out.add("SHOOTING_STAR_SHAPE")
    po,pc=prev["open"],prev["close"]
    if pc<po and cl>op and op<=pc and cl>=po:out.add("BULLISH_ENGULFING")
    if pc>po and cl<op and op>=pc and cl<=po:out.add("BEARISH_ENGULFING")
    if hi<prev["high"] and lo>prev["low"]:out.add("INSIDE_BAR")
    if hi>prev["high"] and lo<prev["low"]:out.add("OUTSIDE_BAR")
    if body/span>=.9:out.add("LONG_BODY")
    return out


def independent_pivots(bars):
    tops,bottoms=[],[]
    for i in range(2,len(bars)-2):
        center=bars[i]
        neighbors=[bars[j] for j in (i-2,i-1,i+1,i+2)]
        if all(center["high"]>b["high"] for b in neighbors):
            tops.append((i,center["high"]))
        if all(center["low"]<b["low"] for b in neighbors):
            bottoms.append((i,center["low"]))
    return tops,bottoms


def _near(a,b):
    return math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-7)


def independently_check(bars, reading, *,frame,as_of):
    if len(bars)!=200:raise AssertionError("Incomplete source history")
    step=PERIOD[frame]*1000
    if any(b["close_time_ms"]-a["close_time_ms"]!=step
           for a,b in zip(bars,bars[1:])):
        raise AssertionError("Noncontiguous timeframes")
    for b in bars:
        op,hi,lo,cl,vol=(b[k] for k in ("open","high","low","close","volume"))
        if not all(math.isfinite(x) for x in (op,hi,lo,cl,vol)):
            raise AssertionError("Nonfinite Gate data")
        if min(op,hi,lo,cl)<=0 or hi<max(op,cl) or lo>min(op,cl) or vol<0:
            raise AssertionError("Invalid source OHLCV")
    if (int(datetime.fromisoformat(reading["last_candle_close_utc"]).timestamp()*1000)
            !=bars[-1]["close_time_ms"]):
        raise AssertionError("Latest candle timestamp mismatch")
    age=as_of.timestamp()-bars[-1]["close_time_ms"]/1000
    if not 0<=age<=2*PERIOD[frame]:
        raise AssertionError("Unclosed or stale newest Gate candle")
    observed=set(reading["candle_patterns"])
    expected=independent_patterns(bars)
    if observed!=expected:
        raise AssertionError("Candle morphology mismatch frame="+frame+
                             " expected="+str(sorted(expected))+
                             " observed="+str(sorted(observed)))
    highs,lows=independent_pivots(bars)
    latest=bars[-1]["close"]
    supports=[v for _,v in lows if v<=latest]
    resistances=[v for _,v in highs if v>=latest]
    support=max(supports) if supports else None
    resistance=min(resistances) if resistances else None
    for key,expected_price in (("support",support),("resistance",resistance)):
        value=reading[key]
        if (value is None)!=(expected_price is None):
            raise AssertionError("Missing or unexpected "+key)
        if value is not None and not _near(value,expected_price):
            raise AssertionError("Incorrect "+key)
    fib=dict(reading["fibonacci_retracements"])
    if highs and lows:
        hi_index,hi=highs[-1]
        lo_index,lo=lows[-1]
        extent=hi-lo
        if extent>0:
            leg="UP" if lo_index<hi_index else "DOWN"
            expected_fib={
                "0.382":hi-extent*.382 if leg=="UP" else lo+extent*.382,
                "0.500":hi-extent*.500 if leg=="UP" else lo+extent*.500,
                "0.618":hi-extent*.618 if leg=="UP" else lo+extent*.618,
            }
        else:expected_fib={}
    else:expected_fib={}
    if set(fib)!=set(expected_fib):
        raise AssertionError("Fibonacci levels absent/extra")
    for key,value in expected_fib.items():
        if not _near(fib[key],value):
            raise AssertionError("Incorrect Fibonacci anchor/level")
    return {"timeframe":frame,"bars":len(bars),"last_close_utc":
        reading["last_candle_close_utc"],"age_seconds":round(age,1),
        "patterns":sorted(observed),"pivots_high":len(highs),
        "pivots_low":len(lows),"confirmed_levels_checked":len(fib),
        "source":"GATE_FUTURES_TESTNET_PUBLIC_GET","execution_authorized":False}


async def main():
    outcomes=[]
    for symbol in MARKETS:
        evidence={}
        async def fetch(s,tf,*,limit):
            if s!=symbol or tf not in PERIOD or limit!=200:
                raise ValueError("Unknown audit market/timeframe")
            rows=await fetch_candles(s,tf,limit=limit)
            evidence[tf]=rows
            return rows
        report=await professional_shadow_snapshot(symbol,fetch=fetch)
        if report["status"]!="READ_ONLY_CHART_READY" or report["agent_signal"]!="NEUTRAL":
            raise AssertionError("Gate SHADOW is not research-ready")
        if report["execution_authorized"] is not False:
            raise AssertionError("Trading is not allowed")
        as_of=datetime.fromisoformat(report["as_of_utc"])
        for r in report["chart_evidence"]["chart_readings"]:
            frame=r["timeframe"]
            result=independently_check(evidence[frame],r,frame=frame,as_of=as_of)
            outcomes.append({"symbol":symbol,**result})
            print("GATE_S1_REAL_CANDLE_VALIDATION "+
                  json.dumps(outcomes[-1],sort_keys=True),flush=True)
    if len(outcomes)!=9:
        raise AssertionError("Missing market/timeframe real candle verification")
    print("GATE_S1_REAL_CANDLE_AUDIT "+
          json.dumps({"tested":len(outcomes),"matched":len(outcomes),
                      "source":"GATE_FUTURES_TESTNET_READ_ONLY",
                      "orders_enabled":False,"profitability_proven":False},
                     sort_keys=True),flush=True)


if __name__=="__main__":
    try:asyncio.run(main())
    except Exception as ex:
        print("GATE_S1_REAL_CANDLE_AUDIT_FAILED "+
              json.dumps({"error_type":type(ex).__name__,
                          "reason":str(ex)[:170],
                          "orders_enabled":False},sort_keys=True),
              file=sys.stderr,flush=True)
        raise SystemExit(2)

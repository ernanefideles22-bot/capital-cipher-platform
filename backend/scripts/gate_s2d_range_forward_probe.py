"""One-shot, GET-only Gate Futures Testnet range probe and forward readiness.

Not scheduled, not a brokerage connection, not a trading bot. This checks
the public range API and records whether NEW candles after frozen policy
boundary can be evaluated without history leakage. All output is diagnostic.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_s2d_forward import (
    FROZEN_AT, POLICY_ID, POLICY_SOURCE_COMMIT,
    evaluate_frozen_forward,
)
from app.strategy.gate_s2d_history import fetch_gate_historical_range

STEPS={"15m":900,"1h":3600,"4h":14400}


def utc_aligned_closed(now,step):
    # Avoid racing recently closed candles not yet served by the venue.
    return datetime.fromtimestamp(
        (int(now.timestamp())-120)//step*step,timezone.utc)


async def main():
    observed=datetime.now(timezone.utc)
    total=0
    for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT"):
        frames={}
        counts={}
        for tf in ("15m","1h","4h"):
            latest=utc_aligned_closed(observed,STEPS[tf])
            # Before policy freeze: validate pagination from >1k candle
            # history and do NOT give it any claim of future performance.
            if tf=="15m":
                bars_back=1250
            else:
                bars_back=180
            first=latest-timedelta(seconds=(bars_back-1)*STEPS[tf])
            raw=await fetch_gate_historical_range(
                symbol,tf,first_closed_at=first,last_closed_at=latest,
                clock=lambda:observed.timestamp(),
            )
            frames[tf]=[
                Candle(exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
                       open=x["open"],high=x["high"],low=x["low"],
                       close=x["close"],volume=x["volume"],
                       closed_at=datetime.fromtimestamp(
                           x["close_time_ms"]/1000,timezone.utc),
                       received_at=observed)
                for x in raw
            ]
            counts[tf]=len(raw)
            total+=len(raw)
        # Never print or infer a forward win rate until frozen policy becomes
        # eligible. The evaluator's explicit status is the only acceptance.
        report=evaluate_frozen_forward(
            frames,observed_at=observed,
        )
        print("GATE_S2D_PUBLIC_RANGE_PROBE "+json.dumps({
            "symbol":symbol,"counts":counts,
            "policy_id":POLICY_ID,"policy_commit":POLICY_SOURCE_COMMIT,
            "freeze_utc":FROZEN_AT.isoformat(),
            "evaluated_at_utc":observed.isoformat(),
            "status":report.status,
            "post_freeze_candles":report.post_freeze_bars,
            "hypothetical_historical_batch":True,
            "order_submission_authorized":report.order_submission_authorized,
            "profitability_proven":False,
        },sort_keys=True),flush=True)
        if any(v!= (1250 if tf=="15m" else 180) for tf,v in counts.items()):
            raise AssertionError("Public Gate range completeness gate")
        if report.order_submission_authorized or report.automatic_promotion_authorized:
            raise AssertionError("Unauthorized S2D promotion")
    print("GATE_S2D_RANGE_CHECK_COMPLETE "+json.dumps({
        "symbols":3,"candles":total,"orders":False,"profitability_proven":False,
    }),flush=True)


if __name__=="__main__":
    asyncio.run(main())

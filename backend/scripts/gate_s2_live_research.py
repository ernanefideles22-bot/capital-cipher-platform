"""One-shot Gate S2 classical strategy smoke on actual public historical candles.

NO KEYS, NO ORDERS, NO OMS, NO PERSISTENCE, NO AUTO-PROMOTION.
200-bar exchange snapshot is too short to establish strategy edge; this
diagnostic explicitly labels point-in-time receipt as unavailable historically.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime,timezone

from app.demo.gate_market import fetch_candles
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_s2_replay import (
    Friction, ResearchRisk, select_then_test_oos,
)

SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT")
FRAMES=("15m","1h","4h")


async def main():
    done=0
    for symbol in SYMBOLS:
        for frame in FRAMES:
            rows=await fetch_candles(symbol,frame,limit=200)
            received=datetime.now(timezone.utc)
            bars=[Candle(exchange=Exchange.GATE,symbol=symbol,timeframe=frame,
                         open=r["open"],high=r["high"],low=r["low"],
                         close=r["close"],volume=r["volume"],
                         closed_at=datetime.fromtimestamp(r["close_time_ms"]/1000,timezone.utc),
                         received_at=received) for r in rows]
            # Explicitly replay a batch snapshot with hypothetical bar-close availability;
            # do not assert historical API delivery or exchange fills.
            report=select_then_test_oos(
                bars,friction=Friction(fee_bps_side=6,slippage_bps_side=6,
                                       funding_bps_8h=1),
                risk=ResearchRisk(min_selection_trades=5,min_oos_evidence_trades=30),
            )
            if report.order_submission_authorized or report.automatic_promotion_authorized:
                raise AssertionError("Historical research has no authority to place trades")
            data={
                "symbol":symbol,"timeframe":frame,"candles":len(bars),
                "train_fraction":.6,
                "selection_status":report.selection_status,
                "selected_strategy":report.selected_strategy,
                "train":[{"name":r.strategy_id,"trades":len(r.trades),
                          "net_pnl":round(r.net_pnl,6),
                          "max_drawdown_pct":round(r.max_drawdown_pct,4)}
                         for r in report.train_reports],
                "oos_trades":len(report.oos_report.trades) if report.oos_report else None,
                "oos_net":round(report.oos_report.net_pnl,6)
                          if report.oos_report else None,
                "execution":False,"automatic_promotion":False,
                "historical_receipt_proven":False,"profitability_proven":False,
                "source":"PUBLIC_GATE_FUTURES_TESTNET_HISTORY",
            }
            print("GATE_S2_HISTORICAL_DIAGNOSTIC "+
                  json.dumps(data,sort_keys=True,allow_nan=False),flush=True)
            done+=1
    assert done==9
    print("GATE_S2_DIAGNOSTIC_COMPLETE "+
          json.dumps({"series":done,"execution":False,
                      "profitability_proven":False}),flush=True)


if __name__=="__main__":
    asyncio.run(main())

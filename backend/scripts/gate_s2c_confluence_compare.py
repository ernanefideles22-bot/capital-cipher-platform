"""S2C one-shot live-public-history comparison: same strategy isolated vs S1-gated.

NO OMS, broker credentials, write endpoints, agent activation or Railway.
Fixed comparison policy chosen BEFORE observing market data. All cases are
diagnostic: this is NOT an OOS-selected trading candidate or alpha proof.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.demo.gate_market import fetch_candles
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_replay import Friction,ResearchRisk,replay_segment
from app.strategy.gate_s2c_confluence import HistoricalConfluenceEvaluator


async def main():
    friction=Friction(fee_bps_side=6,slippage_bps_side=6,funding_bps_8h=1)
    risk=ResearchRisk()
    results=[]
    for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT"):
        received=datetime.now(timezone.utc)
        by_tf={}
        for tf in ("15m","1h","4h"):
            rows=await fetch_candles(symbol,tf,limit=800)
            by_tf[tf]=tuple(Candle(
                exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
                open=r["open"],high=r["high"],low=r["low"],
                close=r["close"],volume=r["volume"],
                closed_at=datetime.fromtimestamp(
                    r["close_time_ms"]/1000,timezone.utc),
                received_at=received,
            ) for r in rows)
        primary=by_tf["15m"]
        split=int(len(primary)*.6)
        for strategy_id in sorted(STRATEGIES):
            # Both arms run the same hypothetical position simulator, costs,
            # entry window, next-open fills, max exposure, and kill switches.
            standalone=replay_segment(
                strategy_id,primary,start_index=split,
                end_index=len(primary),segment="UNSELECTED_DIAGNOSTIC_OOS",
                friction=friction,risk=risk,
            )
            evaluator=HistoricalConfluenceEvaluator({
                "1h":by_tf["1h"],"4h":by_tf["4h"],
            })
            gated=replay_segment(
                strategy_id,primary,start_index=split,
                end_index=len(primary),segment="UNSELECTED_DIAGNOSTIC_OOS",
                friction=friction,risk=risk,
                plan_evaluator=evaluator.evaluate,
            )
            if (standalone.execution_authorized or gated.execution_authorized
                    or len(gated.trades)>len(standalone.trades)):
                raise ValueError("Confluence comparison violated safety contract")
            record={
                "symbol":symbol,"strategy":strategy_id,
                "decision_frame":"15m","context_frames":["1h","4h"],
                "candles_15m":len(primary),"oos_start":split,
                "oos_bars":len(primary)-split,
                "training_selection_performed":False,
                "baseline_trades":len(standalone.trades),
                "confluence_trades":len(gated.trades),
                "baseline_net_pnl":round(standalone.net_pnl,6),
                "confluence_net_pnl":round(gated.net_pnl,6),
                "baseline_drawdown_pct":round(standalone.max_drawdown_pct,6),
                "confluence_drawdown_pct":round(gated.max_drawdown_pct,6),
                "rejected_proposals":evaluator.counts["ENTRIES_REJECTED"],
                "allowed_proposals":evaluator.counts["ENTRIES_ALLOWED"],
                "rejection_reasons":{
                    k:v for k,v in evaluator.counts.items() if k not in
                    ("NO_BASE_ENTRY","BASE_ENTRIES_EVALUATED",
                     "ENTRIES_REJECTED","ENTRIES_ALLOWED")
                },
                "hypothetical_historical_receipts":True,
                "execution_authorized":False,
                "improvement_proven":False,
            }
            print("GATE_S2C_COMPARE "+json.dumps(
                record,sort_keys=True,allow_nan=False),flush=True)
            results.append(record)
    if len(results)!=9:
        raise ValueError("Incomplete paired S1+S2 comparison")
    print("GATE_S2C_COMPARISON_DONE "+json.dumps({
        "comparisons":len(results),
        "series":3,
        "baseline_trades":sum(x["baseline_trades"] for x in results),
        "confluence_trades":sum(x["confluence_trades"] for x in results),
        "execution_authorized":False,
        "improvement_proven":False,
    },sort_keys=True),flush=True)


if __name__=="__main__":
    asyncio.run(main())

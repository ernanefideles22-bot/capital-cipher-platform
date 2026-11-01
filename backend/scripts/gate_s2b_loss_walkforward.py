"""One-shot read-only Gate Testnet historical cost and walk-forward diagnostic.

No exchange credentials, order placement, OMS, DB, runtime mutation or
automatic strategy promotion. 800 recent bars per symbol/timeframe are a
small, overlapping historical snapshot: result is exploratory only.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime,timezone

from app.demo.gate_market import fetch_candles
from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.gate_classical_s2 import STRATEGIES
from app.strategy.gate_s2_loss_diagnostics import attribute, expanding_walk_forward
from app.strategy.gate_s2_replay import Friction, ResearchRisk, replay_segment


async def main():
    friction=Friction(fee_bps_side=6,slippage_bps_side=6,funding_bps_8h=1)
    risk=ResearchRisk(min_selection_trades=5,min_oos_evidence_trades=30)
    summary=[]
    for symbol in ("BTCUSDT","ETHUSDT","SOLUSDT"):
        for tf in ("15m","1h","4h"):
            raw=await fetch_candles(symbol,tf,limit=800)
            observed=datetime.now(timezone.utc)
            candles=[
                Candle(
                    exchange=Exchange.GATE,symbol=symbol,timeframe=tf,
                    open=c["open"],high=c["high"],low=c["low"],
                    close=c["close"],volume=c["volume"],
                    closed_at=datetime.fromtimestamp(
                        c["close_time_ms"]/1000,timezone.utc),
                    received_at=observed)
                for c in raw]
            breakdown=[]
            for sid in sorted(STRATEGIES):
                report=replay_segment(
                    sid,candles,start_index=80,
                    end_index=len(candles),segment="FULL_DIAGNOSTIC_ONLY",
                    friction=friction,risk=risk)
                d=attribute(report,assumptions=friction)
                breakdown.append({
                    "strategy":sid,"trades":d.trades,
                    "pre_slippage_gross":d.raw_gross_before_slippage,
                    "slippage_drag":d.slippage_drag,
                    "fee_drag":d.exchange_fees,
                    "funding_drag":d.funding_estimate,
                    "net":d.net_pnl,"stopped":d.stopped_trades,
                    "targets":d.target_trades,
                    "mark_at_window_end":d.estimated_boundary_exits,
                    "net_expectancy":d.net_expectancy_per_trade,
                })
            wf=expanding_walk_forward(
                candles,initial_train_bars=400,oos_bars=120,
                friction=friction,risk=risk)
            folds=[{
                "fold":f.fold,"train_end":f.training_end_index,
                "oos_start":f.oos_start_index,"oos_end":f.oos_end_index,
                "chosen":f.selected_strategy,
                "oos_completed":sum(t.exit_reason!="END_WINDOW_ESTIMATE"
                                     for t in f.oos_report.trades)
                                if f.oos_report else 0,
                "oos_net":round(f.oos_report.net_pnl,6)
                          if f.oos_report else None,
                "sufficient_evidence":f.sufficient_oos_trades,
            } for f in wf.folds]
            outcome={
                "symbol":symbol,"timeframe":tf,"candles":len(candles),
                "full_history_diagnostics_are_oos":False,
                "cost_breakdown":breakdown,
                "walkforward_folds":folds,
                "folds_selected":wf.folds_with_selection,
                "folds_evidence_sufficient":wf.folds_with_sufficient_oos_evidence,
                "oos_completed":wf.oos_completed_trades,
                "oos_sum_separate_accounts":wf.oos_net_pnl_sum_separate_accounts,
                "execution_authorized":False,
                "profitability_proven":False,
                "historical_receipt_proven":False,
            }
            summary.append(outcome)
            print("GATE_S2B_LOSS_DIAGNOSTIC "+
                  json.dumps(outcome,sort_keys=True,allow_nan=False),
                  flush=True)
    if len(summary)!=9:
        raise RuntimeError("Missing diagnostics")
    print("GATE_S2B_DIAGNOSTIC_COMPLETE "+json.dumps({
        "series":len(summary),
        "folds":sum(len(row["walkforward_folds"]) for row in summary),
        "folds_evidence_sufficient":sum(
            row["folds_evidence_sufficient"] for row in summary),
        "authorized_orders":False,"profitability_proven":False,
    },sort_keys=True),flush=True)


if __name__=="__main__":
    asyncio.run(main())

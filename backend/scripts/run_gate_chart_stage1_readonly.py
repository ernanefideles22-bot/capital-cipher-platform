"""One-shot public Gate FUTURES TestNet chart analysis; zero trade authority.

Never called by backend or agent executor. No credentials are loaded.
Invoke manually only after reviewing API/venue data semantics.
"""
from __future__ import annotations

import asyncio
import json

import httpx

from app.strategy.gate_futures_testnet_feed_stage1 import (
    fetch_gate_testnet_multi_timeframe,
)

CONTRACTS=("BTC_USDT","ETH_USDT","SOL_USDT")


async def run() -> int:
    failures=0
    async with httpx.AsyncClient(timeout=15.0,follow_redirects=False,
                                 trust_env=False) as client:
        for contract in CONTRACTS:
            try:
                report=await fetch_gate_testnet_multi_timeframe(
                    client,contract=contract,
                )
            except (httpx.HTTPError, ValueError) as err:
                failures+=1
                print("S1_GATE_RESEARCH_BLOCKED "+json.dumps({
                    "contract":contract,
                    "error_type":type(err).__name__,
                    "execution_authorized":False,
                },sort_keys=True),flush=True)
                continue
            print("S1_GATE_CHART_SUMMARY "+json.dumps({
                "contract":contract,
                "source":"GATE_FUTURES_TESTNET_PUBLIC_GET_ONLY",
                "observed_as_of_utc":report.as_of.isoformat(),
                "alignment":report.alignment,
                "timeframes":[{
                    "timeframe":r.timeframe,
                    "bars":r.candles_analyzed,
                    "last_close_utc":r.last_close_at.isoformat(),
                    "regime":r.regime,
                    "structure":r.market_structure,
                    "candle_patterns":r.candle_patterns,
                    "chart_patterns":r.chart_patterns,
                    "support":r.support,"resistance":r.resistance,
                    "fibonacci_retracements":r.fibonacci_retracements,
                } for r in report.readings],
                "execution_authorized":False,
                "profitability_proven":False,
                "exchange_source_attested":False,
            },sort_keys=True),flush=True)
    return 0 if failures==0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__=="__main__":
    main()

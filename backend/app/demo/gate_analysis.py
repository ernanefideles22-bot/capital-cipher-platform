"""One-shot, non-trading analysis of Gate Testnet futures market data."""
import asyncio
import json
import math
import os
import sys

from app.demo.gate_market import fetch_candles


class GateAnalysisError(Exception):
    pass


async def analyze_snapshot(symbol, timeframe, equity_usdt, *, fetch=fetch_candles):
    """Evaluate agents on closed Gate candles in a secret-free subprocess.

    Reference price is the last candle close, not a live executable mark price.
    The result is research-only and must never be sent to an executor directly.
    """
    if isinstance(equity_usdt, bool):
        raise GateAnalysisError('invalid virtual equity')
    try:
        equity = float(equity_usdt)
    except (TypeError, ValueError, OverflowError):
        raise GateAnalysisError('invalid virtual equity') from None
    if not math.isfinite(equity) or equity <= 0:
        raise GateAnalysisError('invalid virtual equity')
    rows = await fetch(symbol, timeframe, limit=200)
    if len(rows) < 60:
        raise GateAnalysisError('insufficient candles')
    payload={'exchange':'GATE', 'symbol':symbol, 'timeframe':timeframe,
             'candles':rows, 'price':rows[-1]['close'], 'equity':equity}
    env={k:os.environ[k] for k in ('PATH','PYTHONPATH','LANG') if k in os.environ}
    env.update(PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
    process=await asyncio.create_subprocess_exec(sys.executable, '-m', 'app.demo.analysis',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        out,_=await asyncio.wait_for(process.communicate(json.dumps(payload, allow_nan=False).encode()), timeout=45)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0 or len(out) > 3_000_000:
        raise GateAnalysisError('agent analysis failed')
    try:
        result=json.loads(out)
    except ValueError:
        raise GateAnalysisError('invalid agent output') from None
    if not isinstance(result, dict) or result.get('exchange') != 'GATE' or result.get('market') != 'GATE_USDT_FUTURES_TESTNET':
        raise GateAnalysisError('incorrect data provenance')
    result['reference_price_type']='LAST_CLOSED_CANDLE'
    result['execution_enabled']=False
    result['analysis_scope']='RESEARCH_ONLY'
    return result

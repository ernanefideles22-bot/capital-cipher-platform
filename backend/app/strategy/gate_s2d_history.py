"""S2D: strictly public Gate Futures TestNet historical OHLCV by UTC window.

No credentials, exchange trading client, OMS, persistence, or orders.
Official endpoint GET /futures/usdt/candlesticks: from/to are mutually
exclusive with limit. A historical chunk is checked against its *requested*
close boundary, not falsely represented as data observed at that old time.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from app.demo.gate_market import (
    _ORIGIN, _CONTRACTS, _INTERVALS,
    GateMarketDataError, normalize_candles,
)

MAX_CHUNK_BARS = 500
MAX_WINDOW_BARS = 6000


def _seconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware UTC historical boundary required")
    seconds=value.astimezone(timezone.utc).timestamp()
    if not math.isfinite(seconds) or seconds != int(seconds):
        raise ValueError("Full-second historical boundary required")
    return int(seconds)


async def fetch_gate_historical_range(
    symbol: str, timeframe: str, *,
    first_closed_at: datetime,
    last_closed_at: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
    clock=time.time,
) -> list[dict]:
    """Fetch a *closed-candle* inclusive [first,last] time window.

    Returned rows are normalized public Gate Testnet OHLCV with close_time_ms.
    This is batch-observed history, not verified real-time historical receipts.
    Reject missing bars, duplicates, future closes, and inconsistent metadata.
    No retry storms; at most ceil(6000/500) strictly bounded GET chunks.
    """
    if symbol not in _CONTRACTS or timeframe not in {"15m","1h","4h"}:
        raise GateMarketDataError("unsupported public historical contract")
    step=_INTERVALS[timeframe]
    first,last=_seconds(first_closed_at),_seconds(last_closed_at)
    if (first<=0 or last<first or first%step or last%step
            or (last-first)%step):
        raise ValueError("Unaligned or inverted Gate OHLCV range")
    total=(last-first)//step+1
    if total>MAX_WINDOW_BARS:
        raise ValueError("Historical research window exceeds bounded request budget")
    observed=float(clock())
    if not math.isfinite(observed) or last>observed:
        raise ValueError("Future/unclosed historical candles refused")
    contract=_CONTRACTS[symbol]
    try:
        async with httpx.AsyncClient(
            base_url=_ORIGIN,timeout=12.,follow_redirects=False,
            trust_env=False,transport=transport,
        ) as client:
            meta=await client.get("/api/v4/futures/usdt/contracts/"+contract)
            if meta.status_code!=200:
                raise GateMarketDataError("Gate contract metadata unavailable")
            details=meta.json()
            if not isinstance(details,dict) or details.get("name")!=contract:
                raise GateMarketDataError("Gate contract metadata mismatch")
            try:
                multiplier=Decimal(str(details["quanto_multiplier"]))
            except (InvalidOperation,KeyError,TypeError,ValueError):
                raise GateMarketDataError("Invalid Gate contract multiplier") from None
            if not multiplier.is_finite() or multiplier<=0:
                raise GateMarketDataError("Invalid Gate contract multiplier")
            output=[]
            cursor=first
            while cursor<=last:
                n=min(MAX_CHUNK_BARS,(last-cursor)//step+1)
                upper=cursor+(n-1)*step
                # Gate 't' denotes the OPEN time. Include t=cursor-step
                # ... upper-step. 'to' is exclusive of upper close to avoid
                # including a live candle that has not yet fully closed.
                response=await client.get(
                    "/api/v4/futures/usdt/candlesticks",
                    params={
                        "contract":contract,"interval":timeframe,
                        "from":cursor-step,"to":upper-1,
                    },
                )
                if response.status_code!=200:
                    raise GateMarketDataError("Historical Gate candles unavailable")
                batch=normalize_candles(
                    response.json(),
                    interval_seconds=step,now_seconds=upper,
                    contract_multiplier=multiplier,
                )
                if (len(batch)!=n
                        or batch[0]["close_time_ms"]!=cursor*1000
                        or batch[-1]["close_time_ms"]!=upper*1000):
                    raise GateMarketDataError(
                        "Historical Gate source returned a gap or wrong range"
                    )
                output.extend(batch)
                cursor=upper+step
            if (len(output)!=total or
                    any(b["close_time_ms"]-a["close_time_ms"]!=step*1000
                        for a,b in zip(output,output[1:]))):
                raise GateMarketDataError("Historical Gate range discontinuity")
            return output
    except (httpx.HTTPError,KeyError,TypeError,OverflowError) as exc:
        raise GateMarketDataError("Historical Gate public fetch failed") from None

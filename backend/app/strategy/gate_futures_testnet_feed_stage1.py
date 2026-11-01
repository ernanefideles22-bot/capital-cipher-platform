"""Public GET-only Gate Futures TestNet OHLCV adapter for chart research.

No credentials, signing, order endpoints, writes or background scheduling.
The Gate response 't' is the candle OPEN epoch second; closed_at is t+step.
Bar volume 'v' is venue-supplied futures contract volume, not USDT notional.
A historical HTTP fetch never establishes prospective collection provenance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite

import httpx

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.professional_gate_chart_stage1 import (
    BAR_SECONDS, ProfessionalChartReading, MultiTimeframeReading,
    analyze_gate_chart, analyze_gate_multi_timeframe,
)

# Deliberately no URL parameter. Never send exchange keys to the chart reader.
GATE_FUTURES_TESTNET_BASE="https://api-testnet.gateapi.io/api/v4"
CONTRACT_BY_SYMBOL={"BTCUSDT":"BTC_USDT","ETHUSDT":"ETH_USDT","SOLUSDT":"SOL_USDT"}
ALLOWED_SYMBOLS=frozenset(CONTRACT_BY_SYMBOL)
ALLOWED_CONTRACTS=frozenset(CONTRACT_BY_SYMBOL.values())
SYMBOL_BY_CONTRACT={contract:symbol for symbol,contract in CONTRACT_BY_SYMBOL.items()}
MAX_LIMIT=300


def _require_public_client(client: httpx.AsyncClient) -> None:
    """Prevent inherited Gate keys/cookies from reaching public research GETs."""
    if (client.auth is not None or client.cookies or
            any(name.lower() in {"authorization","key","sign","timestamp","cookie"}
                for name in client.headers)):
        raise ValueError("Authenticated HTTP client forbidden for Gate public research")


def normalize_gate_futures_candles(
    rows: object, *, contract: str, timeframe: str,
    received_at: datetime, volume_multiplier: float | str,
) -> tuple[Candle,...]:
    if contract not in ALLOWED_CONTRACTS or timeframe not in BAR_SECONDS:
        raise ValueError("Unapproved Gate Futures TestNet market/timeframe")
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("Aware received_at clock required")
    if not isinstance(rows,list) or len(rows)>MAX_LIMIT:
        raise ValueError("Invalid or unbounded Gate futures kline response")
    now=received_at.astimezone(timezone.utc)
    try:
        multiplier=Decimal(str(volume_multiplier))
    except (InvalidOperation,ValueError) as ex:
        raise ValueError("Invalid Gate contract volume multiplier") from ex
    if not multiplier.is_finite() or multiplier<=0:
        raise ValueError("Invalid Gate contract volume multiplier")
    step=BAR_SECONDS[timeframe]
    bars=[]
    seen=set()
    for row in rows:
        if not isinstance(row,dict):
            raise ValueError("Invalid Gate futures kline")
        try:
            opened=int(row["t"])
            if isinstance(row["t"],bool) or opened!=float(row["t"]):
                raise ValueError("Invalid Gate timestamp")
            opened_at=datetime.fromtimestamp(opened,tz=timezone.utc)
            if opened % step:
                raise ValueError("Misaligned Gate futures candle")
            closes_at=opened_at+timedelta(seconds=step)
            if closes_at>now:
                # Current open, unfinished bar MUST NOT be analyzed.
                continue
            bar=Candle(
                exchange=Exchange.GATE,symbol=SYMBOL_BY_CONTRACT[contract],timeframe=timeframe,
                open=float(row["o"]),high=float(row["h"]),
                low=float(row["l"]),close=float(row["c"]),
                volume=float(Decimal(str(row["v"]))*multiplier),closed_at=closes_at,
                received_at=now,
            )
        except (KeyError,TypeError,OverflowError,ValueError,InvalidOperation) as ex:
            raise ValueError("Unusable Gate futures kline") from ex
        if opened in seen:
            raise ValueError("Duplicate Gate futures kline")
        seen.add(opened)
        bars.append(bar)
    bars.sort(key=lambda c:c.closed_at)
    return tuple(bars)


async def _get_contract_multiplier(client: httpx.AsyncClient,contract: str) -> float:
    response=await client.get(
        GATE_FUTURES_TESTNET_BASE+"/futures/usdt/contracts/"+contract,
        headers={"Accept":"application/json"},
    )
    response.raise_for_status()
    payload=response.json()
    if not isinstance(payload,dict) or payload.get("name")!=contract:
        raise ValueError("Gate TestNet contract metadata mismatch")
    try:
        multiplier=float(payload["quanto_multiplier"])
    except (TypeError,KeyError,ValueError,OverflowError) as ex:
        raise ValueError("Gate TestNet contract multiplier unavailable") from ex
    if not isfinite(multiplier) or multiplier<=0:
        raise ValueError("Gate TestNet contract multiplier invalid")
    return multiplier


async def fetch_gate_testnet_chart(
    client: httpx.AsyncClient, *, contract: str, timeframe: str,
    requested_limit: int=MAX_LIMIT,
) -> ProfessionalChartReading:
    """One on-demand public GET. Caller owns HTTP client and execution schedule."""
    if contract not in ALLOWED_CONTRACTS or timeframe not in BAR_SECONDS:
        raise ValueError("Unapproved Gate Futures TestNet market/timeframe")
    if not 80<=requested_limit<=MAX_LIMIT:
        raise ValueError("Gate chart history limit must be 80-300")
    _require_public_client(client)
    multiplier=await _get_contract_multiplier(client,contract)
    response=await client.get(
        GATE_FUTURES_TESTNET_BASE+"/futures/usdt/candlesticks",
        params={"contract":contract,"interval":timeframe,"limit":requested_limit},
        headers={"Accept":"application/json"},
    )
    response.raise_for_status()
    observed_at=datetime.now(timezone.utc)
    bars=normalize_gate_futures_candles(
        response.json(),contract=contract,timeframe=timeframe,
        received_at=observed_at, volume_multiplier=multiplier,
    )
    # The chart routine checks contiguous/complete as-of bars and blocks on
    # stale, missing, unreceived or inconsistent candles.
    return analyze_gate_chart(bars,as_of=observed_at)


async def fetch_gate_testnet_multi_timeframe(
    client: httpx.AsyncClient, *,contract: str,
    requested_limit: int=MAX_LIMIT,
) -> MultiTimeframeReading:
    """Three public GETs, never authenticated and never an order instruction."""
    if contract not in ALLOWED_CONTRACTS:
        raise ValueError("Unapproved Gate Futures TestNet contract")
    if not 80<=requested_limit<=MAX_LIMIT:
        raise ValueError("Invalid Gate history limit")
    _require_public_client(client)
    multiplier=await _get_contract_multiplier(client,contract)
    by_tf={}
    for tf in ("4h","1h","15m"):
        response=await client.get(
            GATE_FUTURES_TESTNET_BASE+"/futures/usdt/candlesticks",
            params={"contract":contract,"interval":tf,"limit":requested_limit},
            headers={"Accept":"application/json"},
        )
        response.raise_for_status()
        now=datetime.now(timezone.utc)
        by_tf[tf]=normalize_gate_futures_candles(
            response.json(),contract=contract,timeframe=tf,received_at=now,
            volume_multiplier=multiplier,
        )
    return analyze_gate_multi_timeframe(
        by_tf,as_of=datetime.now(timezone.utc),
    )

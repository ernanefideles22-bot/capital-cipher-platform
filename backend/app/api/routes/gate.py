"""Public, GET-only bridge to the isolated Gate testnet service.

The primary PAPER backend never imports the Gate executor, forwards credentials,
or changes the venue of its own OMS. No arbitrary URL/path forwarding exists.
"""
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException

from app.schemas.api import success_response

router = APIRouter(prefix="/gate", tags=["Gate read-only"])
GATE_ORIGIN = "https://capital-cipher-gate-testnet-staging.up.railway.app"
PUBLIC_PATHS = frozenset({
    "/api/gate/status", "/api/gate/research/forward", "/api/gate/market/candles",
})


async def read_gate(path: str, *, params: dict | None = None) -> dict:
    if path not in PUBLIC_PATHS:
        raise HTTPException(404, "GATE_READ_PATH_NOT_ALLOWED")
    try:
        async with httpx.AsyncClient(
            base_url=GATE_ORIGIN, timeout=8, follow_redirects=False, trust_env=False,
        ) as client:
            response = await client.get(path, params=params)
        if response.is_redirect:
            raise HTTPException(502, "GATE_READ_REDIRECT_BLOCKED")
        if response.status_code != 200:
            raise HTTPException(503, "GATE_READ_UNAVAILABLE")
        if len(response.content) > 2_000_000:
            raise HTTPException(502, "GATE_READ_INVALID_RESPONSE")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Expected an object")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "GATE_READ_UNAVAILABLE") from None
    return result


@router.get("/status")
async def status():
    result = await read_gate("/api/gate/status")
    if result.get("environment") != "GATE_TESTNET":
        raise HTTPException(502, "GATE_READ_IDENTITY_MISMATCH")
    return success_response(result)


@router.get("/research/forward")
async def forward_research():
    return success_response(await read_gate("/api/gate/research/forward"))


@router.get("/market/candles")
async def candles(
    symbol: Literal["BTCUSDT", "ETHUSDT", "SOLUSDT"] = "BTCUSDT",
    timeframe: Literal["15m", "1h", "4h"] = "15m",
):
    result = await read_gate("/api/gate/market/candles", params={
        "symbol": symbol, "timeframe": timeframe,
    })
    if (result.get("source") != "GATE" or result.get("environment") != "GATE_TESTNET"
            or result.get("market") != "GATE_USDT_FUTURES_TESTNET"
            or result.get("symbol") != symbol or result.get("timeframe") != timeframe
            or result.get("closed_candles_only") is not True):
        raise HTTPException(502, "GATE_READ_IDENTITY_MISMATCH")
    return success_response(result)

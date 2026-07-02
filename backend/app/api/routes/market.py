"""Market data endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.context import AppContext
from app.api.deps import get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/market")


@router.get("/symbols")
async def symbols(context: AppContext = Depends(get_context)) -> dict:
    return success_response({"symbols": context.settings.allowed_symbols_list})


@router.get("/candles")
async def candles(
    context: AppContext = Depends(get_context),
    exchange: str = Query(default="BINANCE"),
    symbol: str = Query(default="BTCUSDT"),
    timeframe: str = Query(default="15m"),
    limit: int = Query(default=100, le=500),
) -> dict:
    if symbol.upper() not in context.settings.allowed_symbols_list:
        return error_response("VALIDATION_ERROR", f"Symbol {symbol} not in allowed list")
    data = context.candle_store.get(exchange, symbol, timeframe, limit=limit)
    return success_response({"candles": [c.model_dump(mode="json") for c in data]})


@router.get("/latency")
async def latency(context: AppContext = Depends(get_context)) -> dict:
    latest = None
    for symbol in context.settings.allowed_symbols_list:
        candle = context.candle_store.latest(
            context.settings.default_exchange, symbol, context.settings.default_timeframe
        )
        if candle:
            lag_ms = (candle.received_at - candle.closed_at).total_seconds() * 1000
            latest = {"symbol": symbol, "lag_ms": round(lag_ms, 2)}
            break
    return success_response(
        {"connected": context.market_connected, "last_candle_latency": latest}
    )

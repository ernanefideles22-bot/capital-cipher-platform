"""Orchestrator endpoints (docs/13). /evaluate never sends real orders."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response

router = APIRouter(prefix="/orchestrator")


class EvaluateRequest(BaseModel):
    exchange: str = "BINANCE"
    symbol: str = "BTCUSDT"
    timeframe: str = "15m"


@router.get("/status")
async def orchestrator_status(context: AppContext = Depends(get_context)) -> dict:
    return success_response(context.orchestrator.status())


@router.post("/evaluate", dependencies=[AdminRequired])
async def evaluate(
    body: EvaluateRequest, context: AppContext = Depends(get_context)
) -> dict:
    """Manual PAPER-mode evaluation on the latest stored candle."""
    if body.symbol.upper() not in context.settings.allowed_symbols_list:
        return error_response("VALIDATION_ERROR", f"Symbol {body.symbol} not allowed")
    candle = context.candle_store.latest(body.exchange, body.symbol, body.timeframe)
    if candle is None:
        return error_response(
            "MARKET_DATA_UNAVAILABLE",
            f"No candles stored for {body.symbol} {body.timeframe}",
        )
    decision = await context.orchestrator.on_candle_closed(candle)
    if decision is None:
        return error_response("SYSTEM_NOT_READY", "System cannot evaluate right now")
    return success_response({"decision": decision.model_dump(mode="json")})

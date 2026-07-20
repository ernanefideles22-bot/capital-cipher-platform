"""Market data endpoints (docs/13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.schemas.api import error_response, success_response
from app.schemas.data_catalog import CandleDatasetRequest

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


@router.post("/datasets", dependencies=[AdminRequired])
async def create_dataset(
    body: CandleDatasetRequest,
    context: AppContext = Depends(get_context),
) -> dict:
    if context.data_catalog is None:
        return error_response(
            "DATABASE_UNAVAILABLE",
            "Persistent data catalog is not configured",
        )
    manifest = await context.data_catalog.materialize_candle_dataset(
        exchange=body.exchange.value,
        symbol=body.symbol,
        timeframe=body.timeframe,
        start_at=body.start_at,
        end_at=body.end_at,
        limit=body.limit,
        clock_status=body.clock_status,
    )
    return success_response({"manifest": manifest.model_dump(mode="json")})


@router.get("/datasets/{dataset_hash}", dependencies=[AdminRequired])
async def get_dataset(
    dataset_hash: str = Path(pattern=r"^[a-f0-9]{64}$"),
    context: AppContext = Depends(get_context),
) -> dict:
    if context.repository is None:
        return error_response(
            "DATABASE_UNAVAILABLE",
            "Persistent data catalog is not configured",
        )
    manifest = await context.repository.load_dataset_manifest(dataset_hash)
    if manifest is None:
        return error_response("NOT_FOUND", f"Dataset {dataset_hash} not found")
    return success_response({"manifest": manifest.model_dump(mode="json")})

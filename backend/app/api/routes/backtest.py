"""Backtest and replay endpoints (docs/17, docs/07 Fase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import get_context
from app.market_data.data_quality import validate_raw_candle
from app.schemas.api import error_response, success_response
from app.schemas.backtest import BacktestRequest

router = APIRouter(prefix="/backtest")


@router.post("/run")
async def run_backtest(body: BacktestRequest, context: AppContext = Depends(get_context)) -> dict:
    """Run a backtest from stored candles or inline candle data.

    Never touches live systems: each run uses an isolated pipeline in PAPER mode.
    """
    if body.source == "inline":
        if not body.candles:
            return error_response("VALIDATION_ERROR", "source=inline requires candles[]")
        candles = []
        for raw in body.candles:
            candle, errors = validate_raw_candle(raw)
            if candle is None:
                return error_response(
                    "VALIDATION_ERROR", f"Invalid candle: {errors}", {"candle": raw}
                )
            candles.append(candle)
    elif body.source == "csv":
        if not body.csv_path:
            return error_response("VALIDATION_ERROR", "source=csv requires csv_path")
        from app.market_data.adapters.csv_adapter import CsvMarketDataAdapter

        try:
            candles = CsvMarketDataAdapter(body.csv_path).load_candles()
        except Exception as exc:
            return error_response("VALIDATION_ERROR", f"Failed to load CSV: {exc}")
    else:
        candles = context.candle_store.get(
            body.exchange, body.symbol, body.timeframe, limit=500
        )
    if not candles:
        return error_response(
            "MARKET_DATA_UNAVAILABLE", "No candles available for the requested backtest"
        )
    report = await context.backtesting_engine.run(body, candles)
    return success_response({"report": report.model_dump(mode="json")})


@router.get("/reports")
async def list_reports(context: AppContext = Depends(get_context)) -> dict:
    return success_response(
        {
            "reports": [
                r.model_dump(mode="json", exclude={"equity_curve"})
                for r in context.backtesting_engine.reports
            ]
        }
    )


@router.get("/reports/{backtest_id}")
async def get_report(backtest_id: str, context: AppContext = Depends(get_context)) -> dict:
    for report in context.backtesting_engine.reports:
        if report.backtest_id == backtest_id:
            return success_response({"report": report.model_dump(mode="json")})
    return error_response("NOT_FOUND", f"Backtest {backtest_id} not found")

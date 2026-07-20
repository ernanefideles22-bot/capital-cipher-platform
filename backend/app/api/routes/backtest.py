"""Backtest and replay endpoints (docs/17, docs/07 Fase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.context import AppContext
from app.api.deps import AdminRequired, get_context
from app.core.errors import DataQualityError
from app.market_data.data_quality import validate_raw_candle
from app.schemas.api import error_response, success_response
from app.schemas.backtest import BacktestRequest, WalkForwardRequest
from app.schemas.market import Candle

router = APIRouter(prefix="/backtest")


async def _load_candles(
    body: BacktestRequest,
    context: AppContext,
    *,
    memory_limit: int,
) -> list[Candle]:
    if body.source == "inline":
        if not body.candles:
            raise ValueError("source=inline requires candles[]")
        candles = []
        for raw in body.candles:
            candle, errors = validate_raw_candle(raw)
            if candle is None:
                raise ValueError(f"Invalid candle: {errors}")
            candles.append(candle)
        return candles

    if body.source == "csv":
        if not body.csv_path:
            raise ValueError("source=csv requires csv_path")
        from app.market_data.adapters.csv_adapter import CsvMarketDataAdapter

        try:
            return CsvMarketDataAdapter(body.csv_path).load_candles()
        except Exception as exc:
            raise ValueError(f"Failed to load CSV: {exc}") from exc

    if context.repository is not None:
        candles = await context.repository.list_candles(
            exchange=body.exchange,
            symbol=body.symbol,
            timeframe=body.timeframe,
            limit=100_000,
        )
        if candles and context.data_catalog is not None:
            await context.data_catalog.catalog_candles(candles)
        return candles
    return context.candle_store.get(
        body.exchange,
        body.symbol,
        body.timeframe,
        limit=memory_limit,
    )


@router.post("/run", dependencies=[AdminRequired])
async def run_backtest(body: BacktestRequest, context: AppContext = Depends(get_context)) -> dict:
    """Run a backtest from stored candles or inline candle data.

    Never touches live systems: each run uses an isolated pipeline in PAPER mode.
    """
    try:
        candles = await _load_candles(body, context, memory_limit=500)
    except ValueError as exc:
        return error_response("VALIDATION_ERROR", str(exc))
    if not candles:
        return error_response(
            "MARKET_DATA_UNAVAILABLE", "No candles available for the requested backtest"
        )
    report = await context.backtesting_engine.run(body, candles)
    return success_response({"report": report.model_dump(mode="json")})


@router.post("/walk-forward", dependencies=[AdminRequired])
async def run_walk_forward(
    body: WalkForwardRequest,
    context: AppContext = Depends(get_context),
) -> dict:
    """Run a research-only walk-forward protocol in isolated PAPER pipelines."""

    try:
        candles = await _load_candles(
            body.backtest,
            context,
            memory_limit=100_000,
        )
        if not candles:
            return error_response(
                "MARKET_DATA_UNAVAILABLE",
                "No candles available for the requested walk-forward evaluation",
            )
        report = await context.walk_forward_engine.run(body, candles)
    except (ValueError, DataQualityError) as exc:
        return error_response("VALIDATION_ERROR", str(exc))
    return success_response({"report": report.model_dump(mode="json")})


@router.get("/walk-forward/reports")
async def list_walk_forward_reports(
    context: AppContext = Depends(get_context),
) -> dict:
    reports = await context.walk_forward_engine.list_reports(limit=100)
    return success_response(
        {
            "reports": [
                report.model_dump(mode="json", exclude={"folds"})
                for report in reports
            ]
        }
    )


@router.get("/walk-forward/reports/{experiment_id}")
async def get_walk_forward_report(
    experiment_id: str,
    context: AppContext = Depends(get_context),
) -> dict:
    report = await context.walk_forward_engine.get_report(experiment_id)
    if report is not None:
        return success_response(
            {"report": report.model_dump(mode="json")}
        )
    return error_response(
        "NOT_FOUND",
        f"Walk-forward experiment {experiment_id} not found",
    )


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

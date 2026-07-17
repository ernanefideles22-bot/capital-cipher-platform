"""Capital Cipher AI — FastAPI application entrypoint.

Phase 1: PAPER mode only. No real execution, no private API keys (docs/16).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.context import AppContext, build_context
from app.api.security import ApiSecurityMiddleware
from app.api.routes import (
    agents,
    audit,
    backtest,
    decisions,
    health,
    market,
    orchestrator,
    paper,
    reports,
    risk,
    status,
    strategies,
)
from app.core.config import get_settings
from app.core.errors import CapitalCipherError
from app.core.logging import ServiceLogger, configure_logging
from app.core.state_machine import SystemState
from app.market_data.adapters.binance import BinanceMarketDataAdapter
from app.schemas.api import error_response

logger = ServiceLogger("main")


def create_app(context: AppContext | None = None, *, with_market_data: bool | None = None) -> FastAPI:
    settings = context.settings if context is not None else get_settings()
    configure_logging(settings.log_level)
    if with_market_data is None:
        with_market_data = os.environ.get("ENABLE_MARKET_DATA", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = context or build_context(settings, with_database=bool(settings.database_url))
        app.state.context = ctx
        if ctx.database is not None:
            await ctx.database.create_all()
        # State machine boot: OFFLINE -> INITIALIZING -> PAPER (docs/30).
        await ctx.state_machine.transition(
            SystemState.INITIALIZING, reason="System boot", actor="main"
        )
        await ctx.state_machine.transition(
            SystemState.PAPER, reason="Initialization complete — Phase 1 PAPER mode", actor="main"
        )
        logger.info("System started in PAPER mode", event_type="SYSTEM_STARTED")

        adapter = None
        if with_market_data:
            adapter = BinanceMarketDataAdapter()
            for symbol in ctx.settings.allowed_symbols_list:
                await adapter.subscribe_candles(symbol, ctx.settings.default_timeframe)

            async def on_candle(candle):
                ctx.market_connected = True
                await ctx.orchestrator.on_candle_closed(candle)
                if ctx.repository is not None:
                    await ctx.repository.save_candle(candle)

            async def on_status(event_type: str, payload: dict):
                ctx.market_connected = event_type == "MARKET_CONNECTED"

            adapter.on_candle = on_candle
            adapter.on_status = on_status
            await adapter.connect()

        yield

        if adapter is not None:
            await adapter.disconnect()
        if ctx.database is not None:
            await ctx.database.dispose()
        logger.info("System stopped", event_type="SYSTEM_STOPPED")

    app = FastAPI(
        title="Capital Cipher AI",
        version=settings.app_version,
        description="Institutional multi-agent trading platform — Phase 1 (PAPER only)",
        lifespan=lifespan,
    )
    app.add_middleware(
        ApiSecurityMiddleware,
        requests_per_minute=settings.api_rate_limit_per_minute,
        max_request_body_bytes=settings.max_request_body_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(CapitalCipherError)
    async def domain_error_handler(request: Request, exc: CapitalCipherError) -> JSONResponse:
        return JSONResponse(
            status_code=400, content=error_response(exc.error_code, exc.message, exc.metadata)
        )

    # Root-level health (docs/13).
    app.include_router(health.router)
    # Versioned API (docs/13 base URL /api/v1).
    api_prefix = "/api/v1"
    app.include_router(status.router, prefix=api_prefix)
    app.include_router(market.router, prefix=api_prefix)
    app.include_router(agents.router, prefix=api_prefix)
    app.include_router(orchestrator.router, prefix=api_prefix)
    app.include_router(decisions.router, prefix=api_prefix)
    app.include_router(risk.router, prefix=api_prefix)
    app.include_router(paper.router, prefix=api_prefix)
    app.include_router(audit.router, prefix=api_prefix)
    app.include_router(backtest.router, prefix=api_prefix)
    app.include_router(strategies.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    return app


app = create_app()

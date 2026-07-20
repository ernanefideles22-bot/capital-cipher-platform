"""Capital Cipher AI — FastAPI application entrypoint.

PAPER by default, with an explicitly gated TESTNET OMS. LIVE execution does
not exist.
"""

from __future__ import annotations

import asyncio
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
    oms,
    orchestrator,
    paper,
    reports,
    risk,
    status,
    strategies,
)
from app.core.config import get_settings
from app.core.errors import CapitalCipherError
from app.core.event_bus import Topics
from app.core.logging import ServiceLogger, configure_logging
from app.core.state_machine import SystemState
from app.market_data.adapters.binance import BinanceMarketDataAdapter
from app.schemas.common import Exchange
from app.schemas.api import error_response
from app.schemas.events import EventTypes

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
            if ctx.oms_service.target_environment.value == "TESTNET":
                await ctx.database.verify_testnet_oms_schema()
        await ctx.risk_manager.initialize()
        await ctx.oms_service.initialize()
        if ctx.agent_runtime is not None:
            await ctx.agent_runtime.initialize()
        outbox_stop = asyncio.Event()
        outbox_task = None
        backfill_stop = asyncio.Event()
        backfill_task = None
        agent_worker_stop = asyncio.Event()
        agent_worker_task = None
        oms_worker_stop = asyncio.Event()
        oms_worker_task = None
        reconciliation_stop = asyncio.Event()
        reconciliation_task = None
        if ctx.oms_service.target_environment.value == "TESTNET":
            if not await ctx.oms_service.adapter.healthcheck():
                for execution_adapter in ctx.execution_adapters.values():
                    await execution_adapter.aclose()
                if ctx.database is not None:
                    await ctx.database.dispose()
                raise RuntimeError("Configured TESTNET venue is unavailable")
        if ctx.event_transport is not None:
            try:
                broker_healthy = await ctx.event_transport.healthcheck()
            except Exception as exc:
                broker_healthy = False
                logger.error(
                    "Redis Streams healthcheck failed",
                    event_type="BROKER_UNAVAILABLE",
                    metadata={"error_type": type(exc).__name__},
                )
            if not broker_healthy and settings.event_broker_required:
                await ctx.event_transport.close()
                if ctx.database is not None:
                    await ctx.database.dispose()
                raise RuntimeError("Required Redis Streams broker is unavailable")
            if ctx.outbox_dispatcher is not None:
                outbox_task = asyncio.create_task(
                    ctx.outbox_dispatcher.run(outbox_stop)
                )
        # State machine boot: OFFLINE -> INITIALIZING -> PAPER (docs/30).
        await ctx.state_machine.transition(
            SystemState.INITIALIZING, reason="System boot", actor="main"
        )
        if ctx.risk_manager.control_state.active:
            await ctx.state_machine.transition(
                SystemState.ERROR,
                reason="Durable kill switch active at boot",
                actor="main",
            )
            logger.critical(
                "System started fail-safe with durable kill switch",
                event_type="SYSTEM_START_BLOCKED",
            )
        else:
            await ctx.state_machine.transition(
                SystemState.PAPER,
                reason="Initialization complete - protected OMS boundary",
                actor="main",
            )
            logger.info(
                "System started with protected OMS boundary",
                event_type="SYSTEM_STARTED",
                metadata={
                    "oms_environment": (
                        ctx.oms_service.target_environment.value
                    ),
                    "oms_exchange": ctx.oms_service.target_exchange.value,
                },
            )
        if (
            settings.oms_worker_enabled
            and ctx.oms_service.target_environment.value == "TESTNET"
        ):
            oms_worker_task = asyncio.create_task(
                ctx.oms_service.run(oms_worker_stop)
            )
        if (
            settings.oms_reconciliation_enabled
            and ctx.oms_service.target_environment.value == "TESTNET"
        ):
            reconciliation_task = asyncio.create_task(
                ctx.reconciliation_service.run(reconciliation_stop)
            )
        if (
            settings.agent_worker_enabled
            and ctx.agent_runtime_worker is not None
        ):
            agent_worker_task = asyncio.create_task(
                ctx.agent_runtime_worker.run(agent_worker_stop)
            )
        if settings.backfill_worker_enabled and ctx.backfill_worker is not None:
            backfill_task = asyncio.create_task(
                ctx.backfill_worker.run(backfill_stop)
            )

        adapter = None
        clock_stop = asyncio.Event()
        clock_task = None
        if with_market_data:
            if ctx.clock_monitor is not None:
                try:
                    await ctx.clock_monitor.probe(Exchange.BINANCE)
                except Exception as exc:
                    logger.error(
                        "Initial Binance clock probe failed; normalized ingestion remains blocked",
                        event_type="CLOCK_PROBE_FAILED",
                        metadata={"error_type": type(exc).__name__},
                    )
                clock_task = asyncio.create_task(ctx.clock_monitor.run(clock_stop))
            adapter = BinanceMarketDataAdapter()
            for symbol in ctx.settings.allowed_symbols_list:
                await adapter.subscribe_candles(symbol, ctx.settings.default_timeframe)

            async def on_candle(candle):
                ctx.market_connected = True
                await ctx.orchestrator.on_candle_closed(candle)

            async def on_raw_event(event):
                # Store public source data before normalization or analysis.
                if ctx.repository is not None:
                    await ctx.repository.save_raw_market_event(event)
                await ctx.event_bus.publish(
                    Topics.RAW_MARKET_EVENTS,
                    EventTypes.RAW_MARKET_EVENT_RECEIVED,
                    event.model_dump(mode="json"),
                    source=event.source,
                    correlation_id=event.event_id,
                    event_id=event.event_id,
                )

            async def on_status(event_type: str, payload: dict):
                ctx.market_connected = event_type == "MARKET_CONNECTED"

            adapter.on_candle = on_candle
            adapter.on_raw_event = on_raw_event
            adapter.on_status = on_status
            await adapter.connect()

        yield

        if adapter is not None:
            await adapter.disconnect()
        if clock_task is not None:
            clock_stop.set()
            await clock_task
        if backfill_task is not None:
            backfill_stop.set()
            await backfill_task
        if agent_worker_task is not None:
            agent_worker_stop.set()
            await agent_worker_task
        if reconciliation_task is not None:
            reconciliation_stop.set()
            await reconciliation_task
        if oms_worker_task is not None:
            oms_worker_stop.set()
            await oms_worker_task
        await asyncio.gather(
            *(
                execution_adapter.aclose()
                for execution_adapter in ctx.execution_adapters.values()
            )
        )
        if ctx.public_market_clients is not None:
            await asyncio.gather(
                *(client.aclose() for client in ctx.public_market_clients.values())
            )
        if outbox_task is not None:
            outbox_stop.set()
            await outbox_task
        if ctx.event_transport is not None:
            await ctx.event_transport.close()
        if ctx.database is not None:
            await ctx.database.dispose()
        logger.info("System stopped", event_type="SYSTEM_STOPPED")

    app = FastAPI(
        title="Capital Cipher AI",
        version=settings.app_version,
        description=(
            "Institutional multi-agent platform — PAPER and gated TESTNET OMS"
        ),
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
    app.include_router(oms.router, prefix=api_prefix)
    app.include_router(audit.router, prefix=api_prefix)
    app.include_router(backtest.router, prefix=api_prefix)
    app.include_router(strategies.router, prefix=api_prefix)
    app.include_router(reports.router, prefix=api_prefix)
    return app


app = create_app()

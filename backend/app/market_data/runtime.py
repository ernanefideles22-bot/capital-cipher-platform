"""Explicit runtime market-feed selection for PAPER and gated TESTNET."""

from __future__ import annotations

from app.market_data.adapters.binance import BinanceMarketDataAdapter
from app.market_data.adapters.bybit import (
    BYBIT_TESTNET_WS_URL,
    BybitMarketDataAdapter,
)
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment


def runtime_market_subscriptions(settings, strategy_engine):
    """Use each enabled strategy's actual stream, never invent a 5m strategy."""
    if not settings.office_async_enabled:
        return [(symbol, settings.default_timeframe) for symbol in settings.allowed_symbols_list]
    allowed = set(settings.allowed_symbols_list)
    subscriptions = {(symbol, strategy.timeframe)
        for strategy in strategy_engine.list() if strategy.enabled
        for symbol in strategy.symbols if symbol in allowed}
    return sorted(subscriptions)


def market_data_exchange(oms_service) -> Exchange:
    """Return the exchange whose candles may drive the active OMS boundary."""

    if oms_service.target_environment == ExecutionEnvironment.TESTNET:
        return oms_service.target_exchange
    return Exchange.BINANCE


def build_runtime_market_adapter(oms_service):
    """Build an explicit feed adapter matching the active execution boundary."""

    exchange = market_data_exchange(oms_service)
    if (
        oms_service.target_environment == ExecutionEnvironment.TESTNET
        and exchange == Exchange.BYBIT
    ):
        return BybitMarketDataAdapter(ws_url=BYBIT_TESTNET_WS_URL)
    if exchange == Exchange.BINANCE:
        return BinanceMarketDataAdapter()
    raise ValueError(
        "No public market-data adapter matches the configured OMS boundary"
    )

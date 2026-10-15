"""Runtime market-feed selection tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.market_data.adapters.binance import BinanceMarketDataAdapter
from app.market_data.adapters.bybit import (
    BYBIT_TESTNET_WS_URL,
    BybitMarketDataAdapter,
)
from app.market_data.runtime import (
    build_runtime_market_adapter,
    market_data_exchange,
)
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment


def _oms(environment: ExecutionEnvironment, exchange: Exchange):
    return SimpleNamespace(
        target_environment=environment,
        target_exchange=exchange,
    )


def test_paper_runtime_keeps_binance_public_feed():
    oms = _oms(ExecutionEnvironment.PAPER, Exchange.BINANCE)

    adapter = build_runtime_market_adapter(oms)

    assert market_data_exchange(oms) == Exchange.BINANCE
    assert isinstance(adapter, BinanceMarketDataAdapter)


def test_bybit_testnet_runtime_uses_bybit_testnet_public_feed():
    oms = _oms(ExecutionEnvironment.TESTNET, Exchange.BYBIT)

    adapter = build_runtime_market_adapter(oms)

    assert market_data_exchange(oms) == Exchange.BYBIT
    assert isinstance(adapter, BybitMarketDataAdapter)
    assert adapter.ws_url == BYBIT_TESTNET_WS_URL

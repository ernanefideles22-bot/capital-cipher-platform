from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.errors import RiskError
from app.orchestrator.service import Orchestrator
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment


class FakeRisk:
    def __init__(self):
        self.equity = None

    def update_equity(self, value: float) -> None:
        self.equity = value


class FakeRepository:
    def __init__(self, latest):
        self.latest = latest

    async def load_latest_reconciliation(self, **_kwargs):
        return self.latest


def _orchestrator(*, latest=None, paper_balance=999.0, testnet=True):
    instance = Orchestrator.__new__(Orchestrator)
    instance._paper = SimpleNamespace(balance=paper_balance)
    instance._risk = FakeRisk()
    instance._repository = FakeRepository(latest) if latest is not None else None
    instance._oms = SimpleNamespace(
        target_environment=(
            ExecutionEnvironment.TESTNET
            if testnet
            else ExecutionEnvironment.PAPER
        ),
        target_exchange=Exchange.BYBIT,
    )
    return instance


def _latest(*, equity=1234.5, age_seconds=0, status='MATCHED'):
    observed_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    run = SimpleNamespace(status=SimpleNamespace(value=status))
    balance = SimpleNamespace(
        asset='USDT', equity=equity, observed_at=observed_at
    )
    return run, [], [], [balance]


@pytest.mark.asyncio
async def test_paper_balance_remains_isolated_from_venue_reconciliation():
    orchestrator = _orchestrator(testnet=False, paper_balance=777.0)
    assert await orchestrator._execution_balance() == 777.0


@pytest.mark.asyncio
async def test_testnet_uses_fresh_bybit_usdt_equity_and_updates_drawdown():
    orchestrator = _orchestrator(latest=_latest(equity=4321.0))
    assert await orchestrator._execution_balance() == 4321.0
    assert orchestrator._risk.equity == 4321.0


@pytest.mark.asyncio
async def test_testnet_blocks_without_durable_reconciliation():
    orchestrator = _orchestrator(latest=None)
    with pytest.raises(RiskError, match='durable venue reconciliation'):
        await orchestrator._execution_balance()


@pytest.mark.asyncio
async def test_testnet_blocks_stale_equity():
    orchestrator = _orchestrator(latest=_latest(age_seconds=121))
    with pytest.raises(RiskError, match='stale'):
        await orchestrator._execution_balance()


@pytest.mark.asyncio
async def test_testnet_blocks_failed_reconciliation_or_missing_usdt():
    failed = _orchestrator(latest=_latest(status='FAILED'))
    with pytest.raises(RiskError, match='reconciliation failed'):
        await failed._execution_balance()

    run = SimpleNamespace(status=SimpleNamespace(value='MATCHED'))
    missing = _orchestrator(
        latest=(run, [], [], [SimpleNamespace(
            asset='BTC', equity=1.0, observed_at=datetime.now(timezone.utc)
        )])
    )
    with pytest.raises(RiskError, match='USDT balance missing'):
        await missing._execution_balance()

"""Tests for the reduce-only Bybit TESTNET emergency/canary exit path."""

from __future__ import annotations

import pytest

from app.core.errors import SecurityError
from app.oms.bybit_testnet_exit import BybitTestnetExitService
from app.schemas.common import Exchange, OrderSide
from app.schemas.oms import (
    ExecutionEnvironment,
    VenuePositionSnapshot,
    VenueStateSnapshot,
)


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def record(self, **kwargs) -> None:
        self.events.append(kwargs)


class _Risk:
    def __init__(self, *, active: bool) -> None:
        self.kill_switch_active = active
        self.refresh_count = 0

    async def refresh_positions(self) -> None:
        self.refresh_count += 1


class _Adapter:
    exchange = Exchange.BYBIT

    def __init__(self, states: list[VenueStateSnapshot]) -> None:
        self._states = list(states)
        self.exits: list[tuple[VenuePositionSnapshot, str]] = []
        self.fetch_count = 0

    async def fetch_state(self) -> VenueStateSnapshot:
        self.fetch_count += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    async def submit_reduce_only_exit(
        self,
        position: VenuePositionSnapshot,
        *,
        client_order_id: str,
    ):
        self.exits.append((position, client_order_id))
        return object()


def _position() -> VenuePositionSnapshot:
    return VenuePositionSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=0.001,
        entry_price=100_000.0,
        mark_price=100_100.0,
    )


def _state(*positions: VenuePositionSnapshot) -> VenueStateSnapshot:
    return VenueStateSnapshot(
        exchange=Exchange.BYBIT,
        environment=ExecutionEnvironment.TESTNET,
        positions=list(positions),
    )


@pytest.mark.asyncio
async def test_flatten_requires_central_kill_switch():
    adapter = _Adapter([_state(_position())])
    service = BybitTestnetExitService(
        adapter=adapter,
        risk_manager=_Risk(active=False),
        audit_service=_Audit(),
    )

    with pytest.raises(SecurityError, match="central kill switch"):
        await service.flatten_after_kill_switch(
            correlation_id="canary-1",
            settle_timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )

    assert adapter.exits == []
    assert adapter.fetch_count == 0


@pytest.mark.asyncio
async def test_flatten_submits_one_reduce_only_exit_and_reconciles_flat():
    adapter = _Adapter([_state(_position()), _state()])
    risk = _Risk(active=True)
    audit = _Audit()
    service = BybitTestnetExitService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=audit,
    )

    result = await service.flatten_after_kill_switch(
        correlation_id="canary-2",
        settle_timeout_seconds=0.2,
        poll_interval_seconds=0.01,
    )

    assert result.requested_positions == 1
    assert result.acknowledged_exits == 1
    assert result.reconciliation_flat is True
    assert len(adapter.exits) == 1
    position, client_order_id = adapter.exits[0]
    assert position.symbol == "BTCUSDT"
    assert client_order_id.startswith("cc-exit-")
    assert len(client_order_id) <= 36
    assert risk.refresh_count == 1
    assert [item["audit_type"] for item in audit.events] == [
        "OMS_BYBIT_TESTNET_REDUCE_ONLY_EXIT_REQUESTED",
        "OMS_BYBIT_TESTNET_REDUCE_ONLY_EXIT_ACKNOWLEDGED",
        "OMS_BYBIT_TESTNET_RECONCILIATION_FLAT",
    ]


@pytest.mark.asyncio
async def test_flatten_never_retries_a_write_when_venue_stays_non_flat():
    adapter = _Adapter([_state(_position())])
    risk = _Risk(active=True)
    service = BybitTestnetExitService(
        adapter=adapter,
        risk_manager=risk,
        audit_service=_Audit(),
    )

    with pytest.raises(SecurityError, match="remained non-flat"):
        await service.flatten_after_kill_switch(
            correlation_id="canary-3",
            settle_timeout_seconds=0.02,
            poll_interval_seconds=0.005,
        )

    assert len(adapter.exits) == 1
    assert adapter.fetch_count >= 2
    assert risk.refresh_count == 0


@pytest.mark.asyncio
async def test_flatten_rejects_cross_venue_reconciliation_state():
    wrong_state = VenueStateSnapshot(
        exchange=Exchange.BINANCE,
        environment=ExecutionEnvironment.TESTNET,
    )
    adapter = _Adapter([wrong_state])
    service = BybitTestnetExitService(
        adapter=adapter,
        risk_manager=_Risk(active=True),
        audit_service=_Audit(),
    )

    with pytest.raises(SecurityError, match="did not return Bybit TESTNET"):
        await service.flatten_after_kill_switch(
            correlation_id="canary-4",
            settle_timeout_seconds=0.1,
            poll_interval_seconds=0.01,
        )

    assert adapter.exits == []

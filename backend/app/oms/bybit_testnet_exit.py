"""Fail-closed reduce-only flattening for Bybit Futures TESTNET.

This service deliberately has a narrow mandate: once the central kill switch is
active, reduce reconciled Bybit TESTNET positions to zero without ever opening
or increasing exposure. Writes are never retried blindly; read-only polling is
used to verify that the venue is flat.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from app.core.errors import SecurityError
from app.schemas.common import Exchange
from app.schemas.oms import ExecutionEnvironment, VenuePositionSnapshot


@dataclass(frozen=True)
class BybitFlattenResult:
    requested_positions: int
    acknowledged_exits: int
    reconciliation_flat: bool


class BybitTestnetExitService:
    """Flatten Bybit TESTNET positions only after the kill switch is active."""

    def __init__(self, *, adapter, risk_manager, audit_service) -> None:
        self._adapter = adapter
        self._risk = risk_manager
        self._audit = audit_service

    async def flatten_after_kill_switch(
        self,
        *,
        correlation_id: str,
        settle_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
    ) -> BybitFlattenResult:
        if not self._risk.kill_switch_active:
            raise SecurityError(
                "Bybit TESTNET flatten requires the central kill switch"
            )
        if settle_timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Flatten timing values must be positive")

        initial = await self._adapter.fetch_state()
        self._validate_state(initial)
        positions = sorted(
            (item for item in initial.positions if item.quantity > 0),
            key=lambda item: (item.symbol, item.side.value),
        )
        acknowledged = 0

        for position in positions:
            client_order_id = self._exit_client_order_id(
                correlation_id,
                position,
            )
            await self._audit.record(
                correlation_id=correlation_id,
                audit_type="OMS_BYBIT_TESTNET_REDUCE_ONLY_EXIT_REQUESTED",
                entity_type="venue_position",
                entity_id=f"{position.symbol}:{position.side.value}",
                payload={
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "client_order_id": client_order_id,
                    "reduce_only": True,
                },
            )
            # One write attempt only. Any transport ambiguity is left for venue
            # reconciliation; this method must never duplicate an exit blindly.
            await self._adapter.submit_reduce_only_exit(
                position,
                client_order_id=client_order_id,
            )
            acknowledged += 1
            await self._audit.record(
                correlation_id=correlation_id,
                audit_type="OMS_BYBIT_TESTNET_REDUCE_ONLY_EXIT_ACKNOWLEDGED",
                entity_type="venue_position",
                entity_id=f"{position.symbol}:{position.side.value}",
                payload={
                    "symbol": position.symbol,
                    "client_order_id": client_order_id,
                    "reduce_only": True,
                },
            )

        deadline = time.monotonic() + settle_timeout_seconds
        while True:
            observed = await self._adapter.fetch_state()
            self._validate_state(observed)
            remaining = [
                item for item in observed.positions if item.quantity > 0
            ]
            if not remaining:
                await self._risk.refresh_positions()
                await self._audit.record(
                    correlation_id=correlation_id,
                    audit_type="OMS_BYBIT_TESTNET_RECONCILIATION_FLAT",
                    entity_type="testnet_account",
                    entity_id="BYBIT:TESTNET",
                    payload={
                        "requested_positions": len(positions),
                        "acknowledged_exits": acknowledged,
                    },
                )
                return BybitFlattenResult(
                    requested_positions=len(positions),
                    acknowledged_exits=acknowledged,
                    reconciliation_flat=True,
                )
            if time.monotonic() >= deadline:
                raise SecurityError(
                    "Bybit TESTNET remained non-flat after reduce-only exits"
                )
            await asyncio.sleep(poll_interval_seconds)

    @staticmethod
    def _validate_state(state) -> None:
        if (
            state.exchange != Exchange.BYBIT
            or state.environment != ExecutionEnvironment.TESTNET
        ):
            raise SecurityError(
                "Flatten reconciliation did not return Bybit TESTNET state"
            )

    @staticmethod
    def _exit_client_order_id(
        correlation_id: str,
        position: VenuePositionSnapshot,
    ) -> str:
        identity = (
            f"{correlation_id}:{position.symbol}:"
            f"{position.side.value}:{position.quantity:.12f}"
        )
        token = uuid5(NAMESPACE_URL, identity).hex[:20]
        return f"cc-exit-{token}"

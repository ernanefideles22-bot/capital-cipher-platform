"""Continuous OMS-to-venue reconciliation and conservative drift response."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from app.audit.service import AuditService
from app.core.errors import AuditError
from app.core.logging import ServiceLogger
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.risk.manager import RiskManager
from app.schemas.oms import (
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    ReconciliationMismatch,
    ReconciliationMismatchType,
    ReconciliationRun,
    ReconciliationRunStatus,
    ReconciliationSeverity,
    TERMINAL_OMS_STATUSES,
    VenueStateSnapshot,
)

logger = ServiceLogger("oms_reconciliation")


class ReconciliationService:
    def __init__(
        self,
        *,
        adapter: ExchangeExecutionAdapter,
        risk_manager: RiskManager,
        audit_service: AuditService,
        repository=None,
        halt_on_critical_drift: bool = True,
        interval_seconds: float = 30.0,
    ) -> None:
        self._adapter = adapter
        self._risk = risk_manager
        self._audit = audit_service
        self._repository = repository
        self._halt_on_critical_drift = halt_on_critical_drift
        self._interval_seconds = max(1.0, interval_seconds)
        self._orders: dict[str, OMSOrder] = {}
        self.latest: tuple[
            ReconciliationRun,
            list[ReconciliationMismatch],
            VenueStateSnapshot,
        ] | None = None

    async def reconcile_once(self) -> ReconciliationRun:
        started_at = datetime.now(timezone.utc)
        local_orders = await self._local_orders()
        symbols = {order.symbol for order in local_orders}
        try:
            snapshot = await self._adapter.fetch_state(symbols=symbols)
        except Exception as exc:
            snapshot = VenueStateSnapshot(
                exchange=self._adapter.exchange,
                environment=self._adapter.environment,
            )
            run = ReconciliationRun(
                exchange=self._adapter.exchange,
                environment=self._adapter.environment,
                status=ReconciliationRunStatus.FAILED,
                local_order_count=len(local_orders),
                mismatch_count=1,
                critical_mismatch_count=1,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error_type=type(exc).__name__,
            )
            mismatches = [
                ReconciliationMismatch(
                    run_id=run.run_id,
                    mismatch_type=(
                        ReconciliationMismatchType.ADAPTER_UNAVAILABLE
                    ),
                    severity=ReconciliationSeverity.CRITICAL,
                    exchange=run.exchange,
                    environment=run.environment,
                    expected={"adapter_available": True},
                    observed={
                        "adapter_available": False,
                        "error_type": type(exc).__name__,
                    },
                )
            ]
            await self._persist(run, mismatches, snapshot, [])
            await self._handle_critical(run, mismatches)
            return run

        mismatches, reconciled = _compare_orders(local_orders, snapshot)
        if (
            snapshot.environment == ExecutionEnvironment.TESTNET
            and snapshot.positions
        ):
            reconciled_by_id = {
                order.oms_order_id: order for order in reconciled
            }
            projected_orders = [
                reconciled_by_id.get(order.oms_order_id, order)
                for order in local_orders
            ]
            mismatches.extend(
                _compare_positions(
                    run_exchange=snapshot.exchange,
                    environment=snapshot.environment,
                    local_orders=projected_orders,
                    snapshot=snapshot,
                    run_id="pending",
                )
            )
        critical_count = sum(
            mismatch.severity == ReconciliationSeverity.CRITICAL
            for mismatch in mismatches
        )
        run = ReconciliationRun(
            exchange=snapshot.exchange,
            environment=snapshot.environment,
            status=(
                ReconciliationRunStatus.DRIFT
                if mismatches
                else ReconciliationRunStatus.MATCHED
            ),
            local_order_count=len(local_orders),
            venue_order_count=len(snapshot.orders),
            fill_count=len(snapshot.fills),
            position_count=len(snapshot.positions),
            balance_count=len(snapshot.balances),
            mismatch_count=len(mismatches),
            critical_mismatch_count=critical_count,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
        )
        mismatches = [
            mismatch.model_copy(update={"run_id": run.run_id})
            for mismatch in mismatches
        ]
        await self._persist(run, mismatches, snapshot, reconciled)
        if self._repository is not None:
            await self._risk.refresh_positions()
        if critical_count:
            await self._handle_critical(run, mismatches)
        return run

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.reconcile_once()
            except Exception as exc:
                logger.critical(
                    "OMS reconciliation worker failed closed",
                    event_type="OMS_RECONCILIATION_WORKER_FAILED",
                    metadata={"error_type": type(exc).__name__},
                )
                if not self._risk.kill_switch_active:
                    try:
                        await self._risk.trigger_kill_switch(
                            reason=(
                                "OMS reconciliation worker failure: "
                                f"{type(exc).__name__}"
                            ),
                            actor="oms-reconciliation",
                        )
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                pass

    async def _local_orders(self) -> list[OMSOrder]:
        if self._repository is not None:
            return await self._repository.list_oms_orders(
                exchange=self._adapter.exchange,
                environment=self._adapter.environment,
                limit=None,
            )
        return [
            order
            for order in self._orders.values()
            if order.exchange == self._adapter.exchange
            and order.environment == self._adapter.environment
        ]

    async def _persist(
        self,
        run: ReconciliationRun,
        mismatches: list[ReconciliationMismatch],
        snapshot: VenueStateSnapshot,
        reconciled: list[OMSOrder],
    ) -> None:
        if self._repository is not None:
            await self._repository.persist_reconciliation(
                run,
                mismatches=mismatches,
                snapshot=snapshot,
                reconciled_orders=reconciled,
            )
        else:
            self._orders.update(
                {order.oms_order_id: order for order in reconciled}
            )
        self.latest = (run, mismatches, snapshot)
        try:
            await self._audit.record(
                correlation_id=run.run_id,
                audit_type="OMS_RECONCILIATION_COMPLETED",
                entity_type="reconciliation_run",
                entity_id=run.run_id,
                payload={
                    "exchange": run.exchange.value,
                    "environment": run.environment.value,
                    "status": run.status.value,
                    "mismatch_count": run.mismatch_count,
                    "critical_mismatch_count": (
                        run.critical_mismatch_count
                    ),
                    "error_type": run.error_type,
                },
            )
        except AuditError:
            if not self._risk.kill_switch_active:
                await self._risk.trigger_kill_switch(
                    reason="OMS reconciliation audit persistence failed",
                    actor="oms-reconciliation",
                    correlation_id=run.run_id,
                )

    async def _handle_critical(
        self,
        run: ReconciliationRun,
        mismatches: list[ReconciliationMismatch],
    ) -> None:
        if (
            not self._halt_on_critical_drift
            or self._risk.kill_switch_active
        ):
            return
        await self._risk.trigger_kill_switch(
            reason=(
                f"OMS {run.exchange.value} {run.environment.value} "
                f"reconciliation found {run.critical_mismatch_count} "
                "critical mismatch(es)"
            ),
            actor="oms-reconciliation",
            correlation_id=run.run_id,
        )
        logger.critical(
            "Critical OMS drift activated the kill switch",
            event_type="OMS_CRITICAL_DRIFT",
            correlation_id=run.run_id,
            metadata={
                "mismatch_types": sorted(
                    {
                        mismatch.mismatch_type.value
                        for mismatch in mismatches
                        if mismatch.severity
                        == ReconciliationSeverity.CRITICAL
                    }
                )
            },
        )


def _compare_orders(
    local_orders: list[OMSOrder],
    snapshot: VenueStateSnapshot,
) -> tuple[list[ReconciliationMismatch], list[OMSOrder]]:
    venue_by_client = {
        order.client_order_id: order for order in snapshot.orders
    }
    local_by_client = {
        order.client_order_id: order for order in local_orders
    }
    local_by_venue = {
        order.venue_order_id: order
        for order in local_orders
        if order.venue_order_id is not None
    }
    managed_by_venue = dict(local_by_venue)
    for venue in snapshot.orders:
        managed = local_by_client.get(venue.client_order_id)
        if managed is not None:
            managed_by_venue[venue.venue_order_id] = managed
    local_by_id = {
        order.oms_order_id: order for order in local_orders
    }
    mismatches: list[ReconciliationMismatch] = []
    reconciled: list[OMSOrder] = []

    for local in local_orders:
        venue = venue_by_client.get(local.client_order_id)
        if venue is None:
            if local.status not in {
                OMSOrderStatus.CREATED,
                OMSOrderStatus.PENDING_SUBMISSION,
                *TERMINAL_OMS_STATUSES,
            }:
                mismatches.append(
                    ReconciliationMismatch(
                        run_id="pending",
                        mismatch_type=(
                            ReconciliationMismatchType
                            .LOCAL_ORDER_MISSING_AT_VENUE
                        ),
                        severity=ReconciliationSeverity.CRITICAL,
                        exchange=local.exchange,
                        environment=local.environment,
                        oms_order_id=local.oms_order_id,
                        venue_order_id=local.venue_order_id,
                        symbol=local.symbol,
                        expected={
                            "client_order_id": local.client_order_id,
                            "status": local.status.value,
                        },
                        observed={"present": False},
                    )
                )
            continue
        status_drift = venue.status != local.status
        quantity_drift = (
            abs(
                venue.cumulative_filled_quantity
                - local.cumulative_filled_quantity
            )
            > 1e-10
        )
        identity_drift = (
            local.venue_order_id is not None
            and local.venue_order_id != venue.venue_order_id
        )
        if status_drift:
            mismatches.append(
                ReconciliationMismatch(
                    run_id="pending",
                    mismatch_type=(
                        ReconciliationMismatchType.ORDER_STATUS_DRIFT
                    ),
                    severity=(
                        ReconciliationSeverity.CRITICAL
                        if local.status in TERMINAL_OMS_STATUSES
                        and venue.status not in TERMINAL_OMS_STATUSES
                        else ReconciliationSeverity.WARNING
                    ),
                    exchange=local.exchange,
                    environment=local.environment,
                    oms_order_id=local.oms_order_id,
                    venue_order_id=venue.venue_order_id,
                    symbol=local.symbol,
                    expected={"status": local.status.value},
                    observed={"status": venue.status.value},
                )
            )
        if quantity_drift:
            mismatches.append(
                ReconciliationMismatch(
                    run_id="pending",
                    mismatch_type=(
                        ReconciliationMismatchType.FILLED_QUANTITY_DRIFT
                    ),
                    severity=ReconciliationSeverity.WARNING,
                    exchange=local.exchange,
                    environment=local.environment,
                    oms_order_id=local.oms_order_id,
                    venue_order_id=venue.venue_order_id,
                    symbol=local.symbol,
                    expected={
                        "filled_quantity": local.cumulative_filled_quantity
                    },
                    observed={
                        "filled_quantity": (
                            venue.cumulative_filled_quantity
                        )
                    },
                )
            )
        if identity_drift:
            mismatches.append(
                ReconciliationMismatch(
                    run_id="pending",
                    mismatch_type=(
                        ReconciliationMismatchType.ORDER_STATUS_DRIFT
                    ),
                    severity=ReconciliationSeverity.CRITICAL,
                    exchange=local.exchange,
                    environment=local.environment,
                    oms_order_id=local.oms_order_id,
                    venue_order_id=venue.venue_order_id,
                    symbol=local.symbol,
                    expected={"venue_order_id": local.venue_order_id},
                    observed={"venue_order_id": venue.venue_order_id},
                )
            )
        if (
            local.status not in TERMINAL_OMS_STATUSES
            and (
                status_drift
                or quantity_drift
                or local.venue_order_id is None
            )
        ):
            terminal_at = local.terminal_at
            if venue.status in TERMINAL_OMS_STATUSES:
                terminal_at = terminal_at or snapshot.observed_at
            reconciled.append(
                local.model_copy(
                    update={
                        "status": venue.status,
                        "venue_order_id": venue.venue_order_id,
                        "cumulative_filled_quantity": (
                            venue.cumulative_filled_quantity
                        ),
                        "average_fill_price": venue.average_fill_price,
                        "state_version": local.state_version + 1,
                        "updated_at": snapshot.observed_at,
                        "submitted_at": (
                            local.submitted_at or snapshot.observed_at
                        ),
                        "terminal_at": terminal_at,
                    }
                )
            )

    for venue in snapshot.orders:
        if venue.client_order_id not in local_by_client:
            mismatches.append(
                ReconciliationMismatch(
                    run_id="pending",
                    mismatch_type=(
                        ReconciliationMismatchType.ORPHAN_VENUE_ORDER
                    ),
                    severity=(
                        ReconciliationSeverity.WARNING
                        if venue.status in TERMINAL_OMS_STATUSES
                        else ReconciliationSeverity.CRITICAL
                    ),
                    exchange=venue.exchange,
                    environment=venue.environment,
                    venue_order_id=venue.venue_order_id,
                    symbol=venue.symbol,
                    expected={"managed_by_oms": True},
                    observed={
                        "managed_by_oms": False,
                        "client_order_id": venue.client_order_id,
                    },
                )
            )
    for fill in snapshot.fills:
        managed_order = (
            (
                local_by_client.get(fill.client_order_id)
                if fill.client_order_id is not None
                else None
            )
            or managed_by_venue.get(fill.venue_order_id)
            or (
                local_by_id.get(fill.oms_order_id)
                if fill.oms_order_id is not None
                else None
            )
        )
        if managed_order is None:
            mismatches.append(
                ReconciliationMismatch(
                    run_id="pending",
                    mismatch_type=(
                        ReconciliationMismatchType.ORPHAN_VENUE_FILL
                    ),
                    severity=ReconciliationSeverity.CRITICAL,
                    exchange=fill.exchange,
                    environment=fill.environment,
                    venue_order_id=fill.venue_order_id,
                    symbol=fill.symbol,
                    expected={"managed_by_oms": True},
                    observed={
                        "managed_by_oms": False,
                        "fill_id": fill.fill_id,
                        "client_order_id": fill.client_order_id,
                    },
                )
            )
    return mismatches, reconciled


def _compare_positions(
    *,
    run_exchange,
    environment,
    local_orders: list[OMSOrder],
    snapshot: VenueStateSnapshot,
    run_id: str,
) -> list[ReconciliationMismatch]:
    local_quantities: dict[str, float] = defaultdict(float)
    for order in local_orders:
        if order.cumulative_filled_quantity > 0:
            direction = 1.0 if order.side.value == "BUY" else -1.0
            local_quantities[order.symbol] += (
                direction * order.cumulative_filled_quantity
            )
    venue_quantities: dict[str, float] = defaultdict(float)
    for position in snapshot.positions:
        direction = 1.0 if position.side.value == "BUY" else -1.0
        venue_quantities[position.symbol] += (
            direction * position.quantity
        )
    mismatches = []
    for symbol in sorted(set(local_quantities) | set(venue_quantities)):
        expected = local_quantities.get(symbol, 0.0)
        observed = venue_quantities.get(symbol, 0.0)
        if abs(expected - observed) <= 1e-10:
            continue
        mismatches.append(
            ReconciliationMismatch(
                run_id=run_id,
                mismatch_type=(
                    ReconciliationMismatchType.POSITION_QUANTITY_DRIFT
                ),
                severity=ReconciliationSeverity.CRITICAL,
                exchange=run_exchange,
                environment=environment,
                symbol=symbol,
                expected={
                    "side": (
                        "BUY"
                        if expected > 0
                        else "SELL"
                        if expected < 0
                        else "FLAT"
                    ),
                    "quantity": abs(expected),
                },
                observed={
                    "side": (
                        "BUY"
                        if observed > 0
                        else "SELL"
                        if observed < 0
                        else "FLAT"
                    ),
                    "quantity": abs(observed),
                },
            )
        )
    return mismatches

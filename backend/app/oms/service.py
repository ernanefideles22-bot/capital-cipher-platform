"""Single submission boundary for PAPER and explicitly gated TESTNET orders."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from app.audit.service import AuditService
from app.core.errors import (
    AmbiguousExecutionError,
    AuditError,
    ExecutionRejectedError,
    RiskError,
    SecurityError,
    ValidationError,
)
from app.core.logging import ServiceLogger
from app.execution.adapters.base import ExchangeExecutionAdapter
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.common import Exchange, RiskStatus
from app.schemas.decisions import Decision
from app.schemas.oms import (
    ExecutionCommand,
    ExecutionCommandStatus,
    ExecutionCommandType,
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    OMSTimeInForce,
    TERMINAL_OMS_STATUSES,
    VenueOrderSnapshot,
)
from app.schemas.risk import RiskCheck

logger = ServiceLogger("oms")


class OMSService:
    """Durable, idempotent and deliberately narrow order boundary."""

    def __init__(
        self,
        *,
        target_environment: ExecutionEnvironment,
        target_exchange: Exchange,
        paper_engine: PaperTradingEngine,
        risk_manager: RiskManager,
        audit_service: AuditService,
        adapters: dict[
            tuple[Exchange, ExecutionEnvironment],
            ExchangeExecutionAdapter,
        ],
        repository=None,
        worker_id: str = "oms-worker",
        lease_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if target_environment == ExecutionEnvironment.TESTNET and repository is None:
            raise SecurityError("TESTNET OMS requires durable database persistence")
        adapter_key = (target_exchange, target_environment)
        if adapter_key not in adapters:
            raise SecurityError("OMS target has no explicitly configured adapter")
        self.target_environment = target_environment
        self.target_exchange = target_exchange
        self._paper = paper_engine
        self._risk = risk_manager
        self._audit = audit_service
        self._adapters = adapters
        self._repository = repository
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._orders: dict[str, OMSOrder] = {}
        self._commands: dict[str, ExecutionCommand] = {}

    @property
    def adapter(self) -> ExchangeExecutionAdapter:
        return self._adapters[
            (self.target_exchange, self.target_environment)
        ]

    async def initialize(self) -> None:
        if self._repository is None:
            return
        for order in await self._repository.list_oms_orders(limit=1_000):
            self._orders[order.oms_order_id] = order

    async def submit_approved(
        self,
        decision: Decision,
        risk_check: RiskCheck,
        *,
        current_price: float,
        market_candle=None,
        occurred_at: datetime | None = None,
    ) -> OMSOrder:
        """Submit through PAPER or queue exactly one TESTNET command."""

        if risk_check.risk_status not in {
            RiskStatus.APPROVED,
            RiskStatus.REDUCED,
        }:
            raise RiskError("OMS requires central risk approval")
        if not risk_check.approval_id:
            raise RiskError("OMS requires a single-use approval identity")
        order_id = str(
            uuid5(NAMESPACE_URL, f"paper-order:{risk_check.approval_id}")
            if self.target_environment == ExecutionEnvironment.PAPER
            else uuid5(NAMESPACE_URL, f"oms-order:{risk_check.approval_id}")
        )
        existing = await self.get_order(order_id)
        if existing is not None:
            return existing

        if self.target_environment == ExecutionEnvironment.PAPER:
            paper_order = await self._paper.create_order(
                decision,
                risk_check,
                current_price=current_price,
                market_candle=market_candle,
                occurred_at=occurred_at,
            )
            mirrored = _paper_oms_order(paper_order)
            if self._repository is not None:
                durable = await self._repository.load_oms_order(
                    mirrored.oms_order_id
                )
                if durable is None:
                    raise ValidationError(
                        "Atomic PAPER OMS mirror is missing"
                    )
                mirrored = durable
            self._orders[mirrored.oms_order_id] = mirrored
            return mirrored

        order = OMSOrder(
            oms_order_id=order_id,
            client_order_id=f"cc-{risk_check.approval_id[:32]}",
            decision_id=decision.decision_id,
            risk_check_id=risk_check.risk_check_id,
            approval_id=risk_check.approval_id,
            request_fingerprint=risk_check.request_fingerprint,
            correlation_id=decision.correlation_id,
            exchange=self.target_exchange,
            environment=ExecutionEnvironment.TESTNET,
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            strategy=decision.strategy,
            side=decision.candidate_action.value,
            order_type=OMSOrderType.MARKET,
            time_in_force=OMSTimeInForce.IOC,
            quantity=(risk_check.position_size or 0.0) / current_price,
            requested_notional=risk_check.position_size or 0.0,
            leverage=risk_check.leverage or 1.0,
            reference_price=current_price,
            status=OMSOrderStatus.PENDING_SUBMISSION,
        )
        order = await self.adapter.prepare_order(order)
        command = ExecutionCommand(
            command_id=str(
                uuid5(NAMESPACE_URL, f"oms-submit:{order.oms_order_id}")
            ),
            oms_order_id=order.oms_order_id,
            command_type=ExecutionCommandType.SUBMIT,
            max_attempts=1,
        )
        await self._audit.record(
            correlation_id=order.correlation_id,
            audit_type="OMS_TESTNET_ORDER_REQUESTED",
            entity_type="oms_order",
            entity_id=order.oms_order_id,
            payload={
                "exchange": order.exchange.value,
                "environment": order.environment.value,
                "client_order_id": order.client_order_id,
            },
        )
        created = await self._risk.consume_oms_approval(
            decision,
            risk_check,
            order,
            command,
        )
        self._orders[created.oms_order_id] = created
        if self._repository is None:
            self._commands[command.command_id] = command
        return created

    async def get_order(self, oms_order_id: str) -> OMSOrder | None:
        if self._repository is not None:
            durable = await self._repository.load_oms_order(oms_order_id)
            if durable is not None:
                self._orders[durable.oms_order_id] = durable
                return durable
        return self._orders.get(oms_order_id)

    async def list_orders(self, *, limit: int = 200) -> list[OMSOrder]:
        if self._repository is not None:
            return await self._repository.list_oms_orders(limit=limit)
        return sorted(
            self._orders.values(),
            key=lambda order: order.created_at,
            reverse=True,
        )[:limit]

    async def queue_cancel(self, oms_order_id: str) -> OMSOrder:
        order = await self.get_order(oms_order_id)
        if order is None:
            raise ValidationError("OMS order does not exist")
        if order.environment != ExecutionEnvironment.TESTNET:
            raise SecurityError("Only TESTNET OMS orders can be canceled here")
        if order.status in TERMINAL_OMS_STATUSES:
            raise ValidationError("Terminal OMS order cannot be canceled")
        if order.status not in {
            OMSOrderStatus.SUBMITTED,
            OMSOrderStatus.PARTIALLY_FILLED,
            OMSOrderStatus.CANCEL_PENDING,
            OMSOrderStatus.UNKNOWN,
        }:
            raise ValidationError(
                "TESTNET cancellation requires a venue-acknowledged "
                "or ambiguous order"
            )
        now = datetime.now(timezone.utc)
        updated = order.model_copy(
            update={
                "status": OMSOrderStatus.CANCEL_PENDING,
                "state_version": order.state_version + 1,
                "updated_at": now,
            }
        )
        command = ExecutionCommand(
            command_id=str(
                uuid5(NAMESPACE_URL, f"oms-cancel:{order.oms_order_id}")
            ),
            oms_order_id=order.oms_order_id,
            command_type=ExecutionCommandType.CANCEL,
            max_attempts=1,
        )
        await self._audit.record(
            correlation_id=order.correlation_id,
            audit_type="OMS_TESTNET_CANCEL_REQUESTED",
            entity_type="oms_order",
            entity_id=order.oms_order_id,
        )
        if self._repository is not None:
            updated = await self._repository.queue_cancel_command(
                updated,
                command,
            )
        else:
            self._commands[command.command_id] = command
        self._orders[updated.oms_order_id] = updated
        return updated

    async def dispatch_once(self) -> bool:
        """Execute one leased command, never blindly retrying a write."""

        claimed = await self._claim_command()
        if claimed is None:
            return False
        command, order = claimed
        adapter = self._adapters.get((order.exchange, order.environment))
        if adapter is None or order.environment != ExecutionEnvironment.TESTNET:
            now = datetime.now(timezone.utc)
            updated = order.model_copy(
                update={
                    "status": OMSOrderStatus.UNKNOWN,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                }
            )
            updated = await self._finish_command(
                command,
                updated,
                event_type="EXECUTION_ADAPTER_BLOCKED",
                error_type="SecurityError",
            )
            self._orders[updated.oms_order_id] = updated
            if not self._risk.kill_switch_active:
                await self._risk.trigger_kill_switch(
                    reason="OMS command has no exact TESTNET adapter",
                    actor="oms-worker",
                    correlation_id=order.correlation_id,
                )
            return True
        if (
            command.command_type == ExecutionCommandType.SUBMIT
            and self._risk.kill_switch_active
        ):
            now = datetime.now(timezone.utc)
            updated = order.model_copy(
                update={
                    "status": OMSOrderStatus.QUARANTINED,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "terminal_at": now,
                    "rejection_reason": "KILL_SWITCH_ACTIVE",
                }
            )
            updated = await self._finish_command(
                command,
                updated,
                event_type="SUBMISSION_BLOCKED_BY_KILL_SWITCH",
                error_type="RiskError",
            )
            self._orders[updated.oms_order_id] = updated
            if self._repository is not None:
                try:
                    await self._risk.refresh_positions()
                except Exception:
                    # The kill switch is already active and remains the
                    # fail-safe source of truth.
                    pass
            try:
                await self._audit.record(
                    correlation_id=updated.correlation_id,
                    audit_type=(
                        "OMS_SUBMISSION_BLOCKED_BY_KILL_SWITCH"
                    ),
                    entity_type="oms_order",
                    entity_id=updated.oms_order_id,
                    payload={
                        "status": updated.status.value,
                        "state_version": updated.state_version,
                    },
                )
            except AuditError:
                pass
            return True
        security_violation: str | None = None
        try:
            if command.command_type == ExecutionCommandType.SUBMIT:
                venue = await adapter.submit_order(order)
                event_type = "SUBMISSION_ACKNOWLEDGED"
            else:
                venue = await adapter.cancel_order(order)
                event_type = "CANCELLATION_ACKNOWLEDGED"
            updated = _apply_venue_snapshot(order, venue)
            error_type = None
        except AmbiguousExecutionError as exc:
            now = datetime.now(timezone.utc)
            updated = order.model_copy(
                update={
                    "status": OMSOrderStatus.UNKNOWN,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "rejection_reason": None,
                }
            )
            event_type = "WRITE_STATUS_UNKNOWN"
            error_type = type(exc).__name__
        except ExecutionRejectedError as exc:
            now = datetime.now(timezone.utc)
            if command.command_type == ExecutionCommandType.SUBMIT:
                updates = {
                    "status": OMSOrderStatus.REJECTED,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "terminal_at": now,
                    "rejection_reason": exc.error_code,
                }
                event_type = "WRITE_REJECTED"
            else:
                updates = {
                    "status": OMSOrderStatus.UNKNOWN,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "terminal_at": None,
                    "rejection_reason": None,
                }
                event_type = "CANCEL_REJECTED_RECONCILE_REQUIRED"
            updated = order.model_copy(update=updates)
            error_type = type(exc).__name__
        except SecurityError as exc:
            now = datetime.now(timezone.utc)
            updated = order.model_copy(
                update={
                    "status": OMSOrderStatus.UNKNOWN,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "rejection_reason": None,
                }
            )
            event_type = "VENUE_IDENTITY_REJECTED"
            error_type = type(exc).__name__
            security_violation = exc.message
        except Exception as exc:
            # A transport/library failure around a write is conservatively
            # ambiguous. Reconciliation, not retry, determines venue state.
            now = datetime.now(timezone.utc)
            updated = order.model_copy(
                update={
                    "status": OMSOrderStatus.UNKNOWN,
                    "state_version": order.state_version + 1,
                    "updated_at": now,
                    "rejection_reason": None,
                }
            )
            event_type = "WRITE_STATUS_UNKNOWN"
            error_type = type(exc).__name__
        updated = await self._finish_command(
            command,
            updated,
            event_type=event_type,
            error_type=error_type,
        )
        self._orders[updated.oms_order_id] = updated
        if self._repository is not None:
            try:
                await self._risk.refresh_positions()
            except Exception as exc:
                if not self._risk.kill_switch_active:
                    await self._risk.trigger_kill_switch(
                        reason=(
                            "OMS completed a TESTNET write but portfolio "
                            f"refresh failed: {type(exc).__name__}"
                        ),
                        actor="oms-worker",
                        correlation_id=updated.correlation_id,
                    )
        if (
            security_violation is not None
            and not self._risk.kill_switch_active
        ):
            await self._risk.trigger_kill_switch(
                reason=f"OMS venue identity violation: {security_violation}",
                actor="oms-worker",
                correlation_id=updated.correlation_id,
            )
        try:
            await self._audit.record(
                correlation_id=updated.correlation_id,
                audit_type=f"OMS_{event_type}",
                entity_type="oms_order",
                entity_id=updated.oms_order_id,
                payload={
                    "status": updated.status.value,
                    "state_version": updated.state_version,
                    "error_type": error_type,
                },
            )
        except AuditError:
            if not self._risk.kill_switch_active:
                await self._risk.trigger_kill_switch(
                    reason="OMS post-write audit persistence failed",
                    actor="oms-worker",
                    correlation_id=updated.correlation_id,
                )
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                worked = await self.dispatch_once()
            except Exception as exc:
                worked = False
                logger.critical(
                    "OMS worker failed closed",
                    event_type="OMS_WORKER_FAILED",
                    metadata={"error_type": type(exc).__name__},
                )
                if not self._risk.kill_switch_active:
                    try:
                        await self._risk.trigger_kill_switch(
                            reason=(
                                "OMS worker failure: "
                                f"{type(exc).__name__}"
                            ),
                            actor="oms-worker",
                        )
                    except Exception:
                        # RiskManager always updates the in-process state
                        # machine in its finally block, even if persistence
                        # is unavailable.
                        pass
            if worked:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _claim_command(
        self,
    ) -> tuple[ExecutionCommand, OMSOrder] | None:
        if self._repository is not None:
            return await self._repository.claim_execution_command(
                worker_id=self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        now = datetime.now(timezone.utc)
        for command_id, command in self._commands.items():
            if (
                command.status == ExecutionCommandStatus.PENDING
                and command.attempt_count < command.max_attempts
                and command.available_at <= now
            ):
                leased = command.model_copy(
                    update={
                        "status": ExecutionCommandStatus.LEASED,
                        "attempt_count": command.attempt_count + 1,
                        "leased_by": self._worker_id,
                        "lease_expires_at": (
                            now + timedelta(seconds=self._lease_seconds)
                        ),
                    }
                )
                self._commands[command_id] = leased
                order = self._orders.get(command.oms_order_id)
                if order is None:
                    raise ValidationError("In-memory OMS order is missing")
                return leased, order
        return None

    async def _finish_command(
        self,
        command: ExecutionCommand,
        order: OMSOrder,
        *,
        event_type: str,
        error_type: str | None,
    ) -> OMSOrder:
        if self._repository is not None:
            return await self._repository.finish_execution_command(
                command_id=command.command_id,
                worker_id=self._worker_id,
                order=order,
                event_type=event_type,
                error_type=error_type,
            )
        self._commands[command.command_id] = command.model_copy(
            update={
                "status": ExecutionCommandStatus.COMPLETED,
                "leased_by": None,
                "lease_expires_at": None,
                "last_error_type": error_type,
                "completed_at": order.updated_at,
            }
        )
        return order


def _paper_oms_order(order) -> OMSOrder:
    approval_id = order.approval_id or ("0" * 64)
    timestamp = order.opened_at or order.created_at
    return OMSOrder(
        oms_order_id=order.paper_order_id,
        client_order_id=f"paper-{approval_id[:24]}",
        decision_id=order.decision_id,
        risk_check_id=order.risk_check_id,
        approval_id=approval_id,
        request_fingerprint=order.request_fingerprint,
        correlation_id=order.correlation_id,
        exchange=order.exchange,
        environment=ExecutionEnvironment.PAPER,
        symbol=order.symbol,
        timeframe=order.timeframe or "unknown",
        strategy=order.strategy,
        side=order.side,
        order_type=OMSOrderType.MARKET,
        time_in_force=OMSTimeInForce.IOC,
        quantity=order.position_size / order.entry_price,
        requested_notional=order.position_size,
        leverage=order.leverage,
        reference_price=order.entry_price,
        status=OMSOrderStatus.FILLED,
        venue_order_id=order.paper_order_id,
        cumulative_filled_quantity=order.position_size / order.entry_price,
        average_fill_price=order.entry_price,
        created_at=order.created_at,
        updated_at=timestamp,
        submitted_at=timestamp,
        terminal_at=timestamp,
    )


def _apply_venue_snapshot(
    order: OMSOrder,
    venue: VenueOrderSnapshot,
) -> OMSOrder:
    if (
        venue.exchange != order.exchange
        or venue.environment != order.environment
        or venue.client_order_id != order.client_order_id
        or venue.symbol != order.symbol
    ):
        raise SecurityError("Venue acknowledgement identity does not match OMS")
    now = venue.observed_at
    terminal_at = (
        now if venue.status in TERMINAL_OMS_STATUSES else order.terminal_at
    )
    submitted_at = order.submitted_at
    if venue.status not in {
        OMSOrderStatus.CREATED,
        OMSOrderStatus.PENDING_SUBMISSION,
    }:
        submitted_at = submitted_at or now
    return order.model_copy(
        update={
            "status": venue.status,
            "venue_order_id": venue.venue_order_id,
            "cumulative_filled_quantity": (
                venue.cumulative_filled_quantity
            ),
            "average_fill_price": venue.average_fill_price,
            "state_version": order.state_version + 1,
            "updated_at": now,
            "submitted_at": submitted_at,
            "terminal_at": terminal_at,
        }
    )

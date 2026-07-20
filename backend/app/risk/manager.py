"""Central, fail-safe portfolio risk engine (docs/06, ADR-001)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from app.audit.service import AuditService
from app.core.errors import AuditError, RiskError, SystemStateError, ValidationError
from app.core.logging import ServiceLogger
from app.core.state_machine import SystemState, SystemStateMachine
from app.market_data.store import CandleStore
from app.risk.portfolio import (
    exposure_snapshot,
    portfolio_var,
    remaining_notional_capacity,
)
from app.schemas.common import CandidateAction, OrderSide, RiskStatus
from app.schemas.decisions import Decision
from app.schemas.oms import (
    ExecutionCommand,
    ExecutionEnvironment,
    OMSOrder,
    OMSOrderStatus,
    OMSOrderType,
    OMSTimeInForce,
)
from app.schemas.paper import PaperOrder
from app.schemas.risk import (
    ApprovalStatus,
    OrderApproval,
    PositionExposure,
    RiskCheck,
    RiskControlState,
    RiskLimits,
    RiskState,
)

logger = ServiceLogger("risk_manager")


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _position_snapshot_hash(
    positions: list[PositionExposure],
) -> str:
    return _canonical_hash(
        {
            "positions": sorted(
                (
                    {
                        "paper_order_id": position.paper_order_id,
                        "symbol": position.symbol,
                        "timeframe": position.timeframe,
                        "strategy": position.strategy,
                        "side": position.side.value,
                        "notional": round(position.notional, 8),
                        "leverage": round(position.leverage, 8),
                    }
                    for position in positions
                ),
                key=lambda item: item["paper_order_id"],
            )
        }
    )


class RiskManager:
    """Portfolio-aware risk authority and approval ledger."""

    def __init__(
        self,
        limits: RiskLimits,
        state_machine: SystemStateMachine,
        audit_service: AuditService,
        *,
        initial_balance: float = 10_000.0,
        repository=None,
        candle_store: CandleStore | None = None,
    ) -> None:
        self.limits = limits
        self._sm = state_machine
        self._audit = audit_service
        self._repository = repository
        self._candle_store = candle_store
        self.initial_balance = initial_balance
        self.state = RiskState()
        self.control_state = RiskControlState()
        self._peak_equity = initial_balance
        self._positions: dict[str, PositionExposure] = {}
        self._checks_by_key: dict[str, RiskCheck] = {}
        self._approvals: dict[str, OrderApproval] = {}

    @property
    def kill_switch_active(self) -> bool:
        return (
            self.control_state.active
            or self.state.kill_switch_active
            or self._sm.kill_switch_active
        )

    async def initialize(self) -> None:
        """Restore durable risk control and open PAPER exposure before boot."""

        if self._repository is None:
            return
        control = await self._repository.load_risk_control_state()
        if control is not None:
            self.control_state = control
            if control.active:
                self.state.kill_switch_active = True
                await self._sm.trigger_kill_switch(
                    reason=control.reason or "Durable kill switch active",
                    actor=control.actor or "risk-restore",
                )
        positions = await self._repository.load_open_position_exposures()
        self.sync_positions(positions)

    def sync_positions(self, positions: list[PositionExposure]) -> None:
        self._positions = {
            position.paper_order_id: position for position in positions
        }
        self.set_open_positions(len(self._positions))

    async def refresh_positions(self) -> None:
        """Restore PAPER and reconciled TESTNET exposure from durable evidence."""

        if self._repository is None:
            return
        self.sync_positions(
            await self._repository.load_open_position_exposures()
        )

    def register_position(self, order: PaperOrder) -> None:
        self._positions[order.paper_order_id] = PositionExposure(
            paper_order_id=order.paper_order_id,
            symbol=order.symbol,
            timeframe=order.timeframe or "unknown",
            strategy=order.strategy,
            side=order.side,
            notional=order.position_size,
            leverage=order.leverage,
        )
        self.set_open_positions(len(self._positions))

    def unregister_position(self, paper_order_id: str) -> None:
        self._positions.pop(paper_order_id, None)
        self.set_open_positions(len(self._positions))

    def portfolio_status(self, *, balance: float | None = None) -> dict:
        balance = balance if balance is not None else self.initial_balance
        positions = list(self._positions.values())
        gross = sum(position.notional for position in positions)
        net = sum(
            position.notional
            if position.side == OrderSide.BUY
            else -position.notional
            for position in positions
        )
        by_symbol: dict[str, float] = {}
        by_strategy: dict[str, float] = {}
        for position in positions:
            by_symbol[position.symbol] = (
                by_symbol.get(position.symbol, 0.0) + position.notional
            )
            by_strategy[position.strategy] = (
                by_strategy.get(position.strategy, 0.0)
                + position.notional
            )
        current_var = portfolio_var(
            positions,
            balance=balance,
            limits=self.limits,
            candle_store=self._candle_store,
        )
        return {
            "position_count": len(positions),
            "gross_exposure": round(gross, 8),
            "gross_exposure_percent": round(gross / balance * 100, 8),
            "net_exposure": round(net, 8),
            "net_exposure_percent": round(net / balance * 100, 8),
            "by_symbol": {
                key: round(value, 8)
                for key, value in sorted(by_symbol.items())
            },
            "by_strategy": {
                key: round(value, 8)
                for key, value in sorted(by_strategy.items())
            },
            "var": current_var.model_dump(mode="json"),
        }

    def register_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        self.state.daily_pnl_percent += (pnl / self.initial_balance) * 100

    def set_open_positions(self, count: int) -> None:
        self.state.open_positions = count

    def reset_daily(self) -> None:
        self.state.daily_pnl_percent = 0.0
        self.state.consecutive_losses = 0

    def update_equity(self, balance: float) -> None:
        self._peak_equity = max(self._peak_equity, balance)
        if self._peak_equity > 0:
            self.state.total_drawdown_percent = (
                (self._peak_equity - balance) / self._peak_equity * 100
            )

    async def check(
        self,
        decision: Decision,
        *,
        entry_price: float,
        atr: float | None = None,
        data_quality_score: int = 100,
        market_data_delay_ms: int = 0,
        balance: float | None = None,
        leverage: float | None = None,
        idempotency_key: str | None = None,
        risk_per_trade_percent_override: float | None = None,
        min_risk_reward_override: float | None = None,
        max_open_positions_override: int | None = None,
        max_strategy_exposure_percent_override: float | None = None,
        max_portfolio_var_percent_override: float | None = None,
    ) -> RiskCheck:
        """Evaluate one immutable risk request and mint an execution approval."""

        balance = balance if balance is not None else self.initial_balance
        if entry_price <= 0 or balance <= 0:
            raise ValidationError("Risk evaluation requires positive price and balance")
        leverage = leverage if leverage is not None else self.limits.default_leverage
        effective_risk = min(
            self.limits.risk_per_trade_percent,
            risk_per_trade_percent_override
            if risk_per_trade_percent_override is not None
            else self.limits.risk_per_trade_percent,
        )
        effective_rr = max(
            self.limits.min_risk_reward,
            min_risk_reward_override
            if min_risk_reward_override is not None
            else self.limits.min_risk_reward,
        )
        effective_positions = min(
            self.limits.max_open_positions,
            max_open_positions_override
            if max_open_positions_override is not None
            else self.limits.max_open_positions,
        )
        effective_strategy_exposure = min(
            self.limits.max_strategy_exposure_percent,
            max_strategy_exposure_percent_override
            if max_strategy_exposure_percent_override is not None
            else self.limits.max_strategy_exposure_percent,
        )
        effective_var = min(
            self.limits.max_portfolio_var_percent,
            max_portfolio_var_percent_override
            if max_portfolio_var_percent_override is not None
            else self.limits.max_portfolio_var_percent,
        )
        effective_limits: dict[str, float | int] = {
            "risk_per_trade_percent": effective_risk,
            "min_risk_reward": effective_rr,
            "max_open_positions": effective_positions,
            "max_strategy_exposure_percent": effective_strategy_exposure,
            "max_portfolio_var_percent": effective_var,
            "max_leverage": self.limits.max_leverage,
        }
        key = (idempotency_key or decision.decision_id).strip()
        request = {
            "decision": decision.model_dump(mode="json"),
            "entry_price": entry_price,
            "atr": atr,
            "data_quality_score": data_quality_score,
            "market_data_delay_ms": market_data_delay_ms,
            "balance": balance,
            "leverage": leverage,
            "effective_limits": effective_limits,
        }
        fingerprint = _canonical_hash(request)
        existing = self._checks_by_key.get(key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise ValidationError("Risk idempotency key reused with different input")
            return existing
        if self._repository is not None:
            durable = await self._repository.load_central_risk_evaluation_by_key(
                key
            )
            if durable is not None:
                durable_check, durable_approval = durable
                if durable_check.request_fingerprint != fingerprint:
                    raise ValidationError(
                        "Risk idempotency key reused with different input"
                    )
                self._checks_by_key[key] = durable_check
                if durable_approval is not None:
                    self._approvals[
                        durable_approval.approval_id
                    ] = durable_approval
                return durable_check

        evaluation_id = _canonical_hash(
            {"idempotency_key": key, "request_fingerprint": fingerprint}
        )
        warnings: list[str] = []
        blocked: list[str] = []
        if self._sm.kill_switch_active or self.control_state.active:
            return await self._finalize(
                decision,
                key=key,
                fingerprint=fingerprint,
                evaluation_id=evaluation_id,
                status=RiskStatus.KILL_SWITCH,
                reason=(
                    "Kill switch active: "
                    f"{self._sm.kill_switch_reason or self.control_state.reason}"
                ),
                effective_limits=effective_limits,
            )
        if not self._sm.can_operate():
            blocked.append(
                f"System state {self._sm.state.value} does not allow operation"
            )
        if decision.candidate_action not in (
            CandidateAction.BUY,
            CandidateAction.SELL,
        ):
            blocked.append(
                f"Candidate action {decision.candidate_action.value} is not executable"
            )
        if data_quality_score < 60:
            blocked.append(
                f"Data quality score {data_quality_score} below minimum 60"
            )
        if market_data_delay_ms > self.limits.max_market_data_delay_ms:
            blocked.append(
                f"Market data delay {market_data_delay_ms}ms exceeds limit "
                f"{self.limits.max_market_data_delay_ms}ms"
            )
        if self.state.daily_pnl_percent <= -self.limits.max_daily_drawdown_percent:
            blocked.append("Daily drawdown limit reached")
        if self.state.total_drawdown_percent >= self.limits.max_total_drawdown_percent:
            blocked.append("Total drawdown limit reached")
        if self.state.consecutive_losses >= self.limits.max_consecutive_losses:
            blocked.append("Consecutive loss limit reached")
        observed_position_count = max(
            len(self._positions),
            self.state.open_positions,
        )
        near_position_limit = observed_position_count == effective_positions - 1
        if observed_position_count >= effective_positions:
            blocked.append("Open position limit reached")
        elif near_position_limit:
            warnings.append("NEAR_MAX_OPEN_POSITIONS")
        if leverage > self.limits.max_leverage or leverage < 1:
            blocked.append(
                f"Leverage {leverage} outside allowed range 1..{self.limits.max_leverage}"
            )
        if blocked:
            self.state.blocked_operations += 1
            return await self._finalize(
                decision,
                key=key,
                fingerprint=fingerprint,
                evaluation_id=evaluation_id,
                status=RiskStatus.BLOCKED,
                reason="; ".join(blocked),
                warnings=warnings,
                effective_limits=effective_limits,
            )

        stop_distance = atr * 1.5 if atr and atr > 0 else entry_price * 0.005
        rr_target = max(2.0, effective_rr)
        if decision.candidate_action == CandidateAction.BUY:
            side = OrderSide.BUY
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + stop_distance * rr_target
        else:
            side = OrderSide.SELL
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - stop_distance * rr_target
        risk_amount = balance * effective_risk / 100
        unrestricted_notional = min(
            risk_amount / (stop_distance / entry_price),
            balance * self.limits.max_single_position_percent / 100,
        )
        requested_notional = unrestricted_notional
        if near_position_limit:
            requested_notional *= 0.5
        proposed = PositionExposure(
            paper_order_id=f"proposed:{evaluation_id}",
            symbol=decision.symbol,
            timeframe=decision.timeframe,
            strategy=decision.strategy,
            side=side,
            notional=requested_notional,
            leverage=leverage,
        )
        capacity, binding = remaining_notional_capacity(
            list(self._positions.values()),
            proposed,
            balance=balance,
            limits=self.limits,
            strategy_exposure_limit_percent=effective_strategy_exposure,
        )
        reduced = near_position_limit or capacity + 1e-8 < requested_notional
        if reduced:
            warnings.extend(binding)
            proposed = proposed.model_copy(update={"notional": capacity})
        if proposed.notional <= 0:
            self.state.blocked_operations += 1
            return await self._finalize(
                decision,
                key=key,
                fingerprint=fingerprint,
                evaluation_id=evaluation_id,
                status=RiskStatus.BLOCKED,
                reason="Portfolio exposure capacity exhausted",
                warnings=warnings,
                effective_limits=effective_limits,
            )

        snapshot = exposure_snapshot(
            list(self._positions.values()), proposed, balance=balance
        )
        if (
            self._positions
            and snapshot.symbol_concentration_percent
            > self.limits.max_symbol_concentration_percent
        ):
            self.state.blocked_operations += 1
            return await self._finalize(
                decision,
                key=key,
                fingerprint=fingerprint,
                evaluation_id=evaluation_id,
                status=RiskStatus.BLOCKED,
                reason=(
                    "Symbol concentration "
                    f"{snapshot.symbol_concentration_percent:.2f}% exceeds "
                    f"{self.limits.max_symbol_concentration_percent:.2f}%"
                ),
                warnings=warnings,
                effective_limits=effective_limits,
                portfolio_snapshot=snapshot,
            )

        all_positions = [*self._positions.values(), proposed]
        var_result = portfolio_var(
            all_positions,
            balance=balance,
            limits=self.limits,
            candle_store=self._candle_store,
        )
        warnings.extend(var_result.warnings)
        if var_result.value_at_risk_percent > effective_var:
            low, high = 0.0, proposed.notional
            for _ in range(24):
                middle = (low + high) / 2
                candidate = proposed.model_copy(update={"notional": middle})
                candidate_var = portfolio_var(
                    [*self._positions.values(), candidate],
                    balance=balance,
                    limits=self.limits,
                    candle_store=self._candle_store,
                )
                if candidate_var.value_at_risk_percent <= effective_var:
                    low = middle
                else:
                    high = middle
            if low < 0.01:
                self.state.blocked_operations += 1
                return await self._finalize(
                    decision,
                    key=key,
                    fingerprint=fingerprint,
                    evaluation_id=evaluation_id,
                    status=RiskStatus.BLOCKED,
                    reason=f"Portfolio VaR exceeds {effective_var:.2f}%",
                    warnings=warnings,
                    effective_limits=effective_limits,
                    portfolio_snapshot=snapshot,
                    var_result=var_result,
                )
            proposed = proposed.model_copy(update={"notional": low})
            snapshot = exposure_snapshot(
                list(self._positions.values()), proposed, balance=balance
            )
            var_result = portfolio_var(
                [*self._positions.values(), proposed],
                balance=balance,
                limits=self.limits,
                candle_store=self._candle_store,
            )
            warnings.extend(["PORTFOLIO_VAR_LIMIT", *var_result.warnings])
            reduced = True

        risk_reward = abs(take_profit - entry_price) / abs(
            entry_price - stop_loss
        )
        if risk_reward < effective_rr - 1e-9:
            self.state.blocked_operations += 1
            return await self._finalize(
                decision,
                key=key,
                fingerprint=fingerprint,
                evaluation_id=evaluation_id,
                status=RiskStatus.BLOCKED,
                reason=f"Risk/reward {risk_reward:.2f} below {effective_rr:.2f}",
                warnings=warnings,
                effective_limits=effective_limits,
                portfolio_snapshot=snapshot,
                var_result=var_result,
            )
        status = RiskStatus.REDUCED if reduced else RiskStatus.APPROVED
        return await self._finalize(
            decision,
            key=key,
            fingerprint=fingerprint,
            evaluation_id=evaluation_id,
            status=status,
            reason=(
                "Risk within configured portfolio limits"
                if not reduced
                else "Approved with size reduced by portfolio limits"
            ),
            warnings=sorted(set(warnings)),
            position_size=round(proposed.notional, 8),
            risk_percent=round(
                effective_risk * proposed.notional / unrestricted_notional, 8
            ),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            risk_reward=round(risk_reward, 8),
            effective_limits=effective_limits,
            portfolio_snapshot=snapshot,
            var_result=var_result,
            entry_price=entry_price,
            side=side,
            leverage=leverage,
        )

    async def _finalize(
        self,
        decision: Decision,
        *,
        key: str,
        fingerprint: str,
        evaluation_id: str,
        status: RiskStatus,
        reason: str,
        effective_limits: dict[str, float | int],
        warnings: list[str] | None = None,
        position_size: float | None = None,
        risk_percent: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        risk_reward: float | None = None,
        portfolio_snapshot=None,
        var_result=None,
        entry_price: float | None = None,
        side: OrderSide | None = None,
        leverage: float | None = None,
    ) -> RiskCheck:
        approved = status in (RiskStatus.APPROVED, RiskStatus.REDUCED)
        created_at = datetime.now(timezone.utc)
        risk_check_id = str(uuid5(NAMESPACE_URL, f"risk:{evaluation_id}"))
        approval: OrderApproval | None = None
        approval_id: str | None = None
        if approved:
            approval_id = _canonical_hash(
                {
                    "evaluation_id": evaluation_id,
                    "risk_check_id": risk_check_id,
                    "fingerprint": fingerprint,
                }
            )
            approval = OrderApproval(
                approval_id=approval_id,
                evaluation_id=evaluation_id,
                risk_check_id=risk_check_id,
                decision_id=decision.decision_id,
                correlation_id=decision.correlation_id,
                request_fingerprint=fingerprint,
                position_snapshot_hash=_position_snapshot_hash(
                    list(self._positions.values())
                ),
                symbol=decision.symbol,
                timeframe=decision.timeframe,
                strategy=decision.strategy,
                side=side,
                max_notional=position_size,
                max_leverage=leverage,
                reference_price=entry_price,
                max_entry_deviation_bps=self.limits.max_entry_deviation_bps,
                created_at=created_at,
                expires_at=created_at
                + timedelta(seconds=self.limits.approval_ttl_seconds),
            )
        check = RiskCheck(
            risk_check_id=risk_check_id,
            evaluation_id=evaluation_id,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            decision_id=decision.decision_id,
            correlation_id=decision.correlation_id,
            risk_status=status,
            approved=approved,
            position_size=position_size,
            leverage=leverage if approved else None,
            risk_percent=risk_percent,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=risk_reward,
            reason=reason,
            warnings=warnings or [],
            effective_limits=effective_limits,
            portfolio_snapshot=portfolio_snapshot,
            var_result=var_result,
            approval_id=approval_id,
            created_at=created_at,
        )
        try:
            await self._audit.record(
                correlation_id=decision.correlation_id,
                audit_type="RISK_CHECK",
                entity_type="risk_check",
                entity_id=check.risk_check_id,
                payload=check.model_dump(mode="json"),
            )
            if self._repository is not None:
                await self._repository.save_central_risk_evaluation(
                    check, approval
                )
        except Exception as exc:
            logger.critical(
                "Risk evidence persistence failed; approval withheld",
                event_type="RISK_PERSISTENCE_FAILED",
                correlation_id=decision.correlation_id,
                metadata={"error_type": type(exc).__name__},
            )
            failure_reason = (
                "Audit failure: risk check could not be recorded"
                if isinstance(exc, AuditError)
                else "Risk evidence persistence failure"
            )
            failed = check.model_copy(
                update={
                    "risk_status": RiskStatus.BLOCKED,
                    "approved": False,
                    "approval_id": None,
                    "reason": failure_reason,
                    "warnings": sorted(
                        set([*check.warnings, "RISK_PERSISTENCE_FAILED"])
                    ),
                }
            )
            self._checks_by_key[key] = failed
            self.state.blocked_operations += 1
            return failed
        self._checks_by_key[key] = check
        if approval is not None:
            self._approvals[approval.approval_id] = approval
        return check

    async def consume_approval(
        self,
        decision: Decision,
        risk_check: RiskCheck,
        order: PaperOrder,
        *,
        current_price: float,
    ) -> None:
        """Atomically consume the exact approval used by a PAPER order."""

        approval = self._approvals.get(risk_check.approval_id or "")
        if approval is None:
            raise RiskError("Order approval is unknown or was not issued centrally")
        stored_check = self._checks_by_key.get(risk_check.idempotency_key)
        if stored_check != risk_check:
            raise RiskError("Risk check differs from immutable central evidence")
        now = datetime.now(timezone.utc)
        if approval.status != ApprovalStatus.ACTIVE:
            raise RiskError(f"Order approval is {approval.status.value}")
        if approval.position_snapshot_hash != _position_snapshot_hash(
            list(self._positions.values())
        ):
            raise RiskError("Order approval is stale for the current portfolio")
        if _as_utc(approval.expires_at) <= now:
            self._approvals[approval.approval_id] = approval.model_copy(
                update={"status": ApprovalStatus.EXPIRED}
            )
            raise RiskError("Order approval expired")
        expected = (
            approval.decision_id == decision.decision_id
            and approval.risk_check_id == risk_check.risk_check_id
            and approval.correlation_id == decision.correlation_id
            and approval.symbol == decision.symbol
            and approval.timeframe == decision.timeframe
            and approval.strategy == decision.strategy
            and approval.side == order.side
            and order.position_size <= approval.max_notional + 1e-8
            and order.leverage <= approval.max_leverage + 1e-8
        )
        deviation_bps = (
            abs(current_price - approval.reference_price)
            / approval.reference_price
            * 10_000
        )
        if not expected or deviation_bps > approval.max_entry_deviation_bps:
            raise RiskError("Paper order does not match its central approval")
        if self._sm.kill_switch_active or self.control_state.active:
            raise RiskError("Kill switch prevents approval consumption")
        consumed = approval.model_copy(
            update={
                "status": ApprovalStatus.CONSUMED,
                "consumed_at": now,
                "paper_order_id": order.paper_order_id,
                "oms_order_id": None,
            }
        )
        oms_mirror = OMSOrder(
            oms_order_id=order.paper_order_id,
            client_order_id=f"paper-{approval.approval_id[:24]}",
            decision_id=order.decision_id,
            risk_check_id=order.risk_check_id,
            approval_id=approval.approval_id,
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
            cumulative_filled_quantity=order.position_size
            / order.entry_price,
            average_fill_price=order.entry_price,
            created_at=order.created_at,
            updated_at=order.opened_at or order.created_at,
            submitted_at=order.opened_at or order.created_at,
            terminal_at=order.opened_at or order.created_at,
        )
        if self._repository is not None:
            await self._repository.consume_order_approval(
                consumed,
                order,
                oms_order=oms_mirror,
            )
        self._approvals[approval.approval_id] = consumed

    async def consume_oms_approval(
        self,
        decision: Decision,
        risk_check: RiskCheck,
        order: OMSOrder,
        command: ExecutionCommand,
    ) -> OMSOrder:
        """Atomically authorize and queue one TESTNET OMS command."""

        if order.environment != ExecutionEnvironment.TESTNET:
            raise RiskError("OMS approval consumption is TESTNET-only")
        approval = self._approvals.get(risk_check.approval_id or "")
        if approval is None:
            raise RiskError("Order approval is unknown or was not issued centrally")
        stored_check = self._checks_by_key.get(risk_check.idempotency_key)
        if stored_check != risk_check:
            raise RiskError("Risk check differs from immutable central evidence")
        now = datetime.now(timezone.utc)
        if approval.status != ApprovalStatus.ACTIVE:
            raise RiskError(f"Order approval is {approval.status.value}")
        if approval.position_snapshot_hash != _position_snapshot_hash(
            list(self._positions.values())
        ):
            raise RiskError("Order approval is stale for the current portfolio")
        if _as_utc(approval.expires_at) <= now:
            self._approvals[approval.approval_id] = approval.model_copy(
                update={"status": ApprovalStatus.EXPIRED}
            )
            raise RiskError("Order approval expired")
        expected = (
            approval.decision_id == decision.decision_id
            and approval.risk_check_id == risk_check.risk_check_id
            and approval.correlation_id == decision.correlation_id
            and approval.symbol == decision.symbol
            and approval.timeframe == decision.timeframe
            and approval.strategy == decision.strategy
            and approval.side == order.side
            and order.requested_notional <= approval.max_notional + 1e-8
            and order.leverage <= approval.max_leverage + 1e-8
        )
        deviation_bps = (
            abs(order.reference_price - approval.reference_price)
            / approval.reference_price
            * 10_000
        )
        if not expected or deviation_bps > approval.max_entry_deviation_bps:
            raise RiskError("OMS order does not match its central approval")
        if self._sm.kill_switch_active or self.control_state.active:
            raise RiskError("Kill switch prevents approval consumption")
        if self._repository is not None:
            created = await self._repository.create_oms_order(
                order,
                command=command,
                consume_approval=True,
            )
        else:
            created = order
        remaining_quantity = max(
            0.0,
            created.quantity - created.cumulative_filled_quantity,
        )
        if remaining_quantity > 0:
            self._positions[
                f"oms-reservation:{created.oms_order_id}"
            ] = PositionExposure(
                paper_order_id=(
                    f"oms-reservation:{created.oms_order_id}"
                ),
                symbol=created.symbol,
                timeframe=created.timeframe,
                strategy=created.strategy,
                side=created.side,
                notional=remaining_quantity * created.reference_price,
                leverage=created.leverage,
            )
            self.set_open_positions(len(self._positions))
        self._approvals[approval.approval_id] = approval.model_copy(
            update={
                "status": ApprovalStatus.CONSUMED,
                "consumed_at": now,
                "paper_order_id": None,
                "oms_order_id": order.oms_order_id,
            }
        )
        return created

    async def trigger_kill_switch(
        self,
        *,
        reason: str,
        actor: str,
        correlation_id: str | None = None,
    ) -> None:
        if not reason.strip():
            raise ValidationError("Kill switch reason is required")
        try:
            if self._repository is not None:
                control = await self._repository.set_risk_control(
                    active=True,
                    reason=reason.strip(),
                    actor=actor,
                    correlation_id=correlation_id,
                )
            else:
                control = RiskControlState(
                    active=True,
                    revision=self.control_state.revision + 1,
                    reason=reason.strip(),
                    actor=actor,
                    triggered_at=datetime.now(timezone.utc),
                )
            self.control_state = control
        finally:
            await self._sm.trigger_kill_switch(
                reason=reason.strip(),
                actor=actor,
                correlation_id=correlation_id,
            )
            self.state.kill_switch_active = True
            for approval_id, approval in list(self._approvals.items()):
                if approval.status == ApprovalStatus.ACTIVE:
                    self._approvals[approval_id] = approval.model_copy(
                        update={"status": ApprovalStatus.REVOKED}
                    )
        await self._audit.record(
            correlation_id=correlation_id
            or "00000000-0000-0000-0000-000000000000",
            audit_type="KILL_SWITCH_TRIGGERED",
            entity_type="risk_control",
            payload=self.control_state.model_dump(mode="json"),
        )

    async def reset_kill_switch(
        self,
        *,
        reason: str,
        actor: str,
        correlation_id: str | None = None,
    ) -> None:
        if self._sm.state != SystemState.MAINTENANCE:
            raise SystemStateError(
                "Kill switch reset requires MAINTENANCE state"
            )
        if not reason.strip():
            raise ValidationError("Kill switch reset reason is required")
        if self._repository is not None:
            control = await self._repository.set_risk_control(
                active=False,
                reason=reason.strip(),
                actor=actor,
                correlation_id=correlation_id,
            )
        else:
            control = RiskControlState(
                active=False,
                revision=self.control_state.revision + 1,
                reason=reason.strip(),
                actor=actor,
                reset_at=datetime.now(timezone.utc),
            )
        self.control_state = control
        self._sm.reset_kill_switch_after_maintenance()
        self.state.kill_switch_active = False
        await self._audit.record(
            correlation_id=correlation_id
            or "00000000-0000-0000-0000-000000000000",
            audit_type="KILL_SWITCH_RESET",
            entity_type="risk_control",
            payload=control.model_dump(mode="json"),
        )

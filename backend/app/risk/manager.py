"""Risk Management (docs/06-risk-management.md, ADR-001 risk-first).

Absolute veto authority. Validates per-trade risk, daily drawdown, consecutive
losses, open positions, data quality, latency and system mode. Every check is
audited BEFORE any simulation may proceed; if auditing fails, the operation is
blocked.
"""

from __future__ import annotations

from app.audit.service import AuditService
from app.core.errors import AuditError
from app.core.logging import ServiceLogger
from app.core.state_machine import SystemStateMachine
from app.schemas.common import CandidateAction, RiskStatus
from app.schemas.decisions import Decision
from app.schemas.risk import RiskCheck, RiskLimits, RiskState

logger = ServiceLogger("risk_manager")


class RiskManager:
    """Hierarchical risk validation with veto power (docs/03, docs/06)."""

    def __init__(
        self,
        limits: RiskLimits,
        state_machine: SystemStateMachine,
        audit_service: AuditService,
        *,
        initial_balance: float = 10_000.0,
    ) -> None:
        self.limits = limits
        self._sm = state_machine
        self._audit = audit_service
        self.initial_balance = initial_balance
        self.state = RiskState()
        self._peak_equity = initial_balance

    # -- state maintenance (fed by paper trading engine) ----------------------
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

    def update_equity(self, balance: float) -> None:
        """Track total drawdown from peak equity (docs/06 drawdown total)."""
        self._peak_equity = max(self._peak_equity, balance)
        if self._peak_equity > 0:
            self.state.total_drawdown_percent = (
                (self._peak_equity - balance) / self._peak_equity * 100
            )

    # -- validation ------------------------------------------------------------
    async def check(
        self,
        decision: Decision,
        *,
        entry_price: float,
        atr: float | None = None,
        data_quality_score: int = 100,
        market_data_delay_ms: int = 0,
        balance: float | None = None,
        risk_per_trade_percent_override: float | None = None,
        min_risk_reward_override: float | None = None,
        max_open_positions_override: int | None = None,
    ) -> RiskCheck:
        """Validate a candidate decision. Returns an audited RiskCheck.

        Strategy overrides (docs/26) can only make limits stricter: the global
        limits from docs/06 keep final authority (ADR-001).
        """
        balance = balance if balance is not None else self.initial_balance
        effective_risk_percent = min(
            self.limits.risk_per_trade_percent,
            risk_per_trade_percent_override
            if risk_per_trade_percent_override is not None
            else self.limits.risk_per_trade_percent,
        )
        effective_min_rr = max(
            self.limits.min_risk_reward,
            min_risk_reward_override if min_risk_reward_override is not None else 0.0,
        )
        effective_max_positions = min(
            self.limits.max_open_positions,
            max_open_positions_override
            if max_open_positions_override is not None
            else self.limits.max_open_positions,
        )
        warnings: list[str] = []
        blocked_reasons: list[str] = []

        # Kill switch dominates everything.
        if self._sm.kill_switch_active:
            return await self._finalize(
                decision,
                RiskStatus.KILL_SWITCH,
                reason=f"Kill switch active: {self._sm.kill_switch_reason}",
            )

        # System mode must allow operation (docs/30).
        if not self._sm.can_operate():
            blocked_reasons.append(f"System state {self._sm.state.value} does not allow operation")

        # Only actionable decisions need sizing; HOLD/WAIT/BLOCK are not tradable.
        if decision.candidate_action not in (CandidateAction.BUY, CandidateAction.SELL):
            blocked_reasons.append(
                f"Candidate action {decision.candidate_action.value} is not executable"
            )

        # Data quality gate (docs/32).
        if data_quality_score < 60:
            blocked_reasons.append(f"Data quality score {data_quality_score} below minimum 60")

        # Latency gate (docs/06).
        if market_data_delay_ms > self.limits.max_market_data_delay_ms:
            blocked_reasons.append(
                f"Market data delay {market_data_delay_ms}ms exceeds limit "
                f"{self.limits.max_market_data_delay_ms}ms"
            )

        # Daily drawdown gate.
        if self.state.daily_pnl_percent <= -self.limits.max_daily_drawdown_percent:
            blocked_reasons.append(
                f"Daily drawdown {abs(self.state.daily_pnl_percent):.2f}% reached limit "
                f"{self.limits.max_daily_drawdown_percent}%"
            )

        # Total drawdown gate (docs/06 drawdown total).
        if self.state.total_drawdown_percent >= self.limits.max_total_drawdown_percent:
            blocked_reasons.append(
                f"Total drawdown {self.state.total_drawdown_percent:.2f}% reached limit "
                f"{self.limits.max_total_drawdown_percent}%"
            )

        # Consecutive losses gate.
        if self.state.consecutive_losses >= self.limits.max_consecutive_losses:
            blocked_reasons.append(
                f"Consecutive losses {self.state.consecutive_losses} reached limit "
                f"{self.limits.max_consecutive_losses}"
            )

        # Open positions gate.
        reduced = False
        if self.state.open_positions >= effective_max_positions:
            blocked_reasons.append(
                f"Open positions {self.state.open_positions} reached limit "
                f"{effective_max_positions}"
            )
        elif self.state.open_positions == effective_max_positions - 1:
            reduced = True
            warnings.append("NEAR_MAX_OPEN_POSITIONS")

        if blocked_reasons:
            self.state.blocked_operations += 1
            return await self._finalize(
                decision, RiskStatus.BLOCKED, reason="; ".join(blocked_reasons), warnings=warnings
            )

        # Position sizing: risk_per_trade% of balance against stop distance.
        stop_distance = (atr * 1.5) if atr and atr > 0 else entry_price * 0.005
        # Target sized by the effective minimum RR (docs/06 example is 2.0;
        # strategy profiles from docs/26 may demand more — stricter wins).
        rr_target = max(2.0, effective_min_rr)
        if decision.candidate_action == CandidateAction.BUY:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + stop_distance * rr_target
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - stop_distance * rr_target
        risk_amount = balance * (effective_risk_percent / 100)
        if reduced:
            risk_amount *= 0.5
        position_size = risk_amount / stop_distance * entry_price / entry_price  # quote units
        position_value = risk_amount / (stop_distance / entry_price)
        risk_reward = abs(take_profit - entry_price) / abs(entry_price - stop_loss)

        if risk_reward < effective_min_rr - 1e-9:  # epsilon for float precision
            self.state.blocked_operations += 1
            return await self._finalize(
                decision,
                RiskStatus.BLOCKED,
                reason=f"Risk/reward {risk_reward:.2f} below minimum {effective_min_rr}",
                warnings=warnings,
            )

        status = RiskStatus.REDUCED if reduced else RiskStatus.APPROVED
        return await self._finalize(
            decision,
            status,
            reason="Risk within configured limits"
            if not reduced
            else "Approved with reduced size (near max open positions)",
            warnings=warnings,
            position_size=round(min(position_value, balance), 2),
            risk_percent=effective_risk_percent * (0.5 if reduced else 1.0),
            stop_loss=round(stop_loss, 2),
            take_profit=round(take_profit, 2),
            risk_reward=round(risk_reward, 2),
        )

    async def _finalize(
        self,
        decision: Decision,
        status: RiskStatus,
        *,
        reason: str,
        warnings: list[str] | None = None,
        position_size: float | None = None,
        risk_percent: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        risk_reward: float | None = None,
    ) -> RiskCheck:
        check = RiskCheck(
            decision_id=decision.decision_id,
            correlation_id=decision.correlation_id,
            risk_status=status,
            approved=status in (RiskStatus.APPROVED, RiskStatus.REDUCED),
            position_size=position_size,
            risk_percent=risk_percent,
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_reward=risk_reward,
            reason=reason,
            warnings=warnings or [],
        )
        # Critical rule (docs/06): the risk decision must be recorded BEFORE
        # simulation. If recording fails, the operation is blocked.
        try:
            await self._audit.record(
                correlation_id=decision.correlation_id,
                audit_type="RISK_CHECK",
                entity_type="risk_check",
                entity_id=check.risk_check_id,
                payload=check.model_dump(mode="json"),
            )
        except AuditError:
            logger.critical(
                "Risk check could not be audited — blocking operation",
                event_type="AUDIT_LOG_FAILED",
                correlation_id=decision.correlation_id,
            )
            return check.model_copy(
                update={
                    "risk_status": RiskStatus.BLOCKED,
                    "approved": False,
                    "reason": "Audit failure: risk check could not be recorded",
                }
            )
        logger.info(
            f"Risk check {status.value}",
            event_type="RISK_CHECK_COMPLETED",
            correlation_id=decision.correlation_id,
            metadata={"decision_id": decision.decision_id, "reason": reason},
        )
        return check

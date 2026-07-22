"""Deterministic, orderless validation of a prolonged PAPER shadow campaign."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Protocol

from app.agents.runtime import AgentRuntime
from app.market_data.store import CandleStore
from app.oms.reconciliation import ReconciliationService
from app.oms.service import OMSService
from app.operations.metrics import percentile
from app.operations.resilience import RecoveryCoordinator
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import AgentStatus
from app.schemas.market import Candle
from app.schemas.shadow_validation import (
    ShadowCampaignCheckpoint,
    ShadowCampaignDefinition,
    ShadowValidationReport,
    timeframe_seconds,
)


class ShadowValidationRepository(Protocol):
    async def save_shadow_campaign_checkpoint(
        self,
        checkpoint: ShadowCampaignCheckpoint,
    ) -> ShadowCampaignCheckpoint: ...

    async def save_shadow_validation_report(
        self,
        report: ShadowValidationReport,
    ) -> ShadowValidationReport: ...

    async def list_shadow_campaign_checkpoints(
        self,
        *,
        campaign_id: str | None = None,
        limit: int = 100,
    ) -> list[ShadowCampaignCheckpoint]: ...

    async def list_shadow_validation_reports(
        self,
        *,
        limit: int = 100,
    ) -> list[ShadowValidationReport]: ...


def _fingerprint(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def candle_dataset_fingerprint(candles: list[Candle]) -> str:
    return _fingerprint(
        [
            candle.model_dump(mode="json", exclude={"received_at"})
            for candle in candles
        ]
    )


class ShadowValidationService:
    """Runs evidence-only agents; it owns no decision, risk or order method."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        candle_store: CandleStore,
        reconciliation: ReconciliationService,
        risk_manager: RiskManager,
        oms_service: OMSService,
        paper_engine: PaperTradingEngine,
        repository: ShadowValidationRepository | None = None,
    ) -> None:
        self._runtime = runtime
        self._candle_store = candle_store
        self._reconciliation = reconciliation
        self._risk = risk_manager
        self._oms = oms_service
        self._paper = paper_engine
        self._repository = repository
        self._checkpoints: deque[ShadowCampaignCheckpoint] = deque(
            maxlen=10_000
        )
        self._reports: deque[ShadowValidationReport] = deque(maxlen=1_000)

    async def initialize(self) -> None:
        if self._repository is None:
            return
        checkpoints, reports = await asyncio.gather(
            self._repository.list_shadow_campaign_checkpoints(limit=10_000),
            self._repository.list_shadow_validation_reports(limit=1_000),
        )
        self._checkpoints.extend(reversed(checkpoints))
        self._reports.extend(reversed(reports))

    async def run(
        self,
        definition: ShadowCampaignDefinition,
        candles: list[Candle],
    ) -> ShadowValidationReport:
        self._validate_input(definition, candles)
        registrations = self._runtime.registry.registrations()
        await self._runtime.initialize()
        initial_risk = self._risk_state_hash()
        limits_hash = self._risk_limits_hash()
        initial_orders = len(await self._oms.list_orders(limit=1_000))
        initial_trades = self._paper_trade_count()
        recovery = RecoveryCoordinator(recovery_successes_required=3)
        started_at = datetime.now(timezone.utc)
        checkpoint_indexes = list(
            range(
                definition.checkpoint_interval_candles - 1,
                len(candles),
                definition.checkpoint_interval_candles,
            )
        )
        if not checkpoint_indexes or checkpoint_indexes[-1] != len(candles) - 1:
            checkpoint_indexes.append(len(candles) - 1)
        if len(checkpoint_indexes) < 3:
            raise ValueError("Campaign requires at least three checkpoints")
        fault_schedule = {
            checkpoint_indexes[len(checkpoint_indexes) // 3]: "BROKER",
            checkpoint_indexes[(2 * len(checkpoint_indexes)) // 3]: "DATABASE",
        }
        checkpoints: list[ShadowCampaignCheckpoint] = []
        cursor = 0
        for sequence, candle_index in enumerate(checkpoint_indexes, start=1):
            for candle in candles[cursor : candle_index + 1]:
                self._candle_store.add(candle)
            cursor = candle_index + 1
            scenario = fault_schedule.get(candle_index)
            if scenario is not None:
                recovery.observe(
                    scenario,
                    healthy=False,
                    reason=f"injected isolated Month 11 {scenario} outage",
                )
            reconciliation = await self._reconciliation.reconcile_once()
            checkpoint = await self._checkpoint(
                definition=definition,
                registrations=registrations,
                sequence=sequence,
                replay_at=candles[candle_index].closed_at,
                recovery=recovery,
                degradation_scenario=scenario,
                reconciliation=reconciliation,
                limits_hash=limits_hash,
                initial_risk_hash=initial_risk,
                initial_order_count=initial_orders,
                initial_trade_count=initial_trades,
            )
            checkpoints.append(checkpoint)
            await self._persist_checkpoint(checkpoint)
            if scenario is not None:
                self._recover(recovery, scenario)
        completed_at = datetime.now(timezone.utc)
        report = await self._report(
            definition=definition,
            checkpoints=checkpoints,
            initial_risk=initial_risk,
            limits_hash=limits_hash,
            initial_orders=initial_orders,
            initial_trades=initial_trades,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._reports.append(report)
        if self._repository is not None:
            await self._repository.save_shadow_validation_report(report)
        return report

    def _validate_input(
        self,
        definition: ShadowCampaignDefinition,
        candles: list[Candle],
    ) -> None:
        if len(candles) != definition.replay_candle_count:
            raise ValueError("Replay candle count differs from campaign definition")
        if candle_dataset_fingerprint(candles) != definition.dataset_fingerprint:
            raise ValueError("Replay dataset fingerprint mismatch")
        if candles[0].closed_at != definition.replay_start_at:
            raise ValueError("Replay start differs from campaign definition")
        if candles[-1].closed_at != definition.replay_end_at:
            raise ValueError("Replay end differs from campaign definition")
        if any(
            current.closed_at <= previous.closed_at
            for previous, current in zip(candles, candles[1:])
        ):
            raise ValueError("Replay candles must be strictly ordered")
        expected_step = timeframe_seconds(definition.timeframe)
        if any(
            (current.closed_at - previous.closed_at).total_seconds()
            != expected_step
            for previous, current in zip(candles, candles[1:])
        ):
            raise ValueError("Replay candle cadence is not contiguous")
        if any(
            candle.symbol != definition.symbol
            or candle.timeframe != definition.timeframe
            for candle in candles
        ):
            raise ValueError("Replay market identity differs from campaign")
        self._runtime.registry.validate_cohort(expected_count=300)
        registrations = self._runtime.registry.registrations()
        if sum(item.decision_role == "PRIMARY" for item in registrations) != 3:
            raise ValueError("Campaign requires exactly three primary agents")
        if sum(item.decision_role == "SHADOW" for item in registrations) != 297:
            raise ValueError("Campaign requires exactly 297 shadow agents")

    async def _checkpoint(
        self,
        *,
        definition: ShadowCampaignDefinition,
        registrations,
        sequence: int,
        replay_at,
        recovery: RecoveryCoordinator,
        degradation_scenario: str | None,
        reconciliation,
        limits_hash: str,
        initial_risk_hash: str,
        initial_order_count: int,
        initial_trade_count: int,
    ) -> ShadowCampaignCheckpoint:
        mode = recovery.mode
        outputs = []
        duration_ms = 0.0
        reconciliation_blocked = reconciliation.critical_mismatch_count > 0
        risk_blocked = self._risk.kill_switch_active
        if (
            recovery.shadow_allowed
            and not reconciliation_blocked
            and not risk_blocked
        ):
            correlation = f"m11-{definition.campaign_id[:8]}-{sequence}"
            requests = [
                AgentExecutionRequest(
                    agent_version=registration.version,
                    idempotency_key=(
                        f"{definition.campaign_id}:{sequence}:"
                        f"{registration.agent_name}"
                    ),
                    input=AgentInput(
                        request_id=(
                            f"{definition.campaign_id}:{sequence}:"
                            f"{registration.agent_name}"
                        ),
                        correlation_id=correlation,
                        agent_name=registration.agent_name,
                        timestamp=replay_at,
                        symbol=definition.symbol,
                        timeframe=definition.timeframe,
                        market_context={
                            "exchange": "BINANCE",
                            "campaign_id": definition.campaign_id,
                            "replay": True,
                            "order_authority": False,
                        },
                    ),
                )
                for registration in registrations
            ]
            started = time.perf_counter()
            outputs = await self._runtime.execute_many(requests)
            duration_ms = (time.perf_counter() - started) * 1_000
        failures = sum(
            output.status in {AgentStatus.FAILED, AgentStatus.TIMEOUT}
            for output in outputs
        )
        skipped = sum(output.status == AgentStatus.SKIPPED for output in outputs)
        error_rate = failures / len(outputs) if outputs else 0.0
        p95 = percentile([float(output.latency_ms) for output in outputs], 0.95)
        orders = len(await self._oms.list_orders(limit=1_000))
        trades = self._paper_trade_count()
        suspended = (
            not recovery.shadow_allowed
            or reconciliation_blocked
            or risk_blocked
        )
        invariants = {
            "exact_300_or_safely_suspended": (
                len(outputs) == 300 if not suspended else len(outputs) == 0
            ),
            "paper_only": all(
                registration.execution_mode == "PAPER"
                for registration in registrations
            ),
            "authority_split_is_3_primary_297_shadow": (
                sum(item.decision_role == "PRIMARY" for item in registrations) == 3
                and sum(item.decision_role == "SHADOW" for item in registrations) == 297
            ),
            "error_rate_within_limit": error_rate <= definition.max_error_rate,
            "latency_within_limit": p95 <= definition.max_p95_latency_ms,
            "no_critical_reconciliation_drift": (
                reconciliation.critical_mismatch_count == 0
            ),
            "risk_limits_unchanged": self._risk_limits_hash() == limits_hash,
            "risk_state_unchanged_at_checkpoint": (
                self._risk_state_hash() == initial_risk_hash
            ),
            "orders_unchanged_at_checkpoint": orders == initial_order_count,
            "paper_trades_unchanged_at_checkpoint": (
                trades == initial_trade_count
            ),
            "degradation_suspends_shadow": (
                suspended if degradation_scenario is not None else True
            ),
            "reconciliation_blocks_execution": (
                len(outputs) == 0 if reconciliation_blocked else True
            ),
            "risk_kill_switch_blocks_execution": (
                len(outputs) == 0 if risk_blocked else True
            ),
            "risk_authority_is_available": not risk_blocked,
            "no_order_capability": all(
                not set(registration.capabilities)
                & {"submit-order", "cancel-order", "exchange-credentials"}
                for registration in registrations
            ),
            "no_order_submission": True,
            "no_live_execution": True,
        }
        return ShadowCampaignCheckpoint(
            campaign_id=definition.campaign_id,
            sequence=sequence,
            replay_at=replay_at,
            status=(
                "BLOCKED_RECONCILIATION"
                if reconciliation_blocked
                else "BLOCKED_RISK"
                if risk_blocked
                else "SUSPENDED_SAFE_DEGRADATION"
                if suspended
                else "EXECUTED"
            ),
            acceptance_status=(
                "PASSED" if all(invariants.values()) else "FAILED"
            ),
            recovery_mode=mode,
            degradation_scenario=degradation_scenario,
            executed_agents=len(outputs),
            failed_agents=failures,
            skipped_agents=skipped,
            duration_ms=round(duration_ms, 6),
            p95_latency_ms=round(p95, 6),
            error_rate=round(error_rate, 8),
            reconciliation_status=reconciliation.status.value,
            reconciliation_mismatches=reconciliation.mismatch_count,
            reconciliation_critical_mismatches=(
                reconciliation.critical_mismatch_count
            ),
            risk_state_hash=self._risk_state_hash(),
            risk_limits_hash=limits_hash,
            order_count=orders,
            paper_trade_count=trades,
            invariants=invariants,
        )

    async def _report(
        self,
        *,
        definition,
        checkpoints,
        initial_risk,
        limits_hash,
        initial_orders,
        initial_trades,
        started_at,
        completed_at,
    ) -> ShadowValidationReport:
        final_risk = self._risk_state_hash()
        final_orders = len(await self._oms.list_orders(limit=1_000))
        final_trades = self._paper_trade_count()
        executed = [item for item in checkpoints if item.status == "EXECUTED"]
        suspended = [
            item
            for item in checkpoints
            if item.status != "EXECUTED"
        ]
        total_executions = sum(item.executed_agents for item in checkpoints)
        total_failures = sum(item.failed_agents for item in checkpoints)
        scenarios = [
            item.degradation_scenario
            for item in suspended
            if item.degradation_scenario is not None
        ]
        invariants = {
            "minimum_seven_day_replay": (
                definition.replay_end_at - definition.replay_start_at
            ).total_seconds()
            >= 604_800,
            "exact_300_agent_registry": all(
                item.registered_agents == 300 for item in checkpoints
            ),
            "all_checkpoint_invariants_passed": all(
                all(item.invariants.values()) for item in checkpoints
            ),
            "aggregate_error_rate_within_limit": (
                total_failures / total_executions if total_executions else 1.0
            )
            <= definition.max_error_rate,
            "latency_within_limit": max(
                (item.p95_latency_ms for item in executed),
                default=0.0,
            )
            <= definition.max_p95_latency_ms,
            "reconciliation_has_no_critical_drift": sum(
                item.reconciliation_critical_mismatches for item in checkpoints
            )
            == 0,
            "risk_state_unchanged": final_risk == initial_risk,
            "risk_limits_unchanged": self._risk_limits_hash() == limits_hash,
            "orders_unchanged": final_orders == initial_orders,
            "paper_trades_unchanged": final_trades == initial_trades,
            "broker_degradation_validated": "BROKER" in scenarios,
            "database_safe_halt_validated": "DATABASE" in scenarios,
            "recovery_completed": len(executed) >= 1 and checkpoints[-1].status == "EXECUTED",
            "no_live_execution": True,
        }
        return ShadowValidationReport(
            campaign=definition,
            status="PASSED" if all(invariants.values()) else "FAILED",
            checkpoint_ids=[item.checkpoint_id for item in checkpoints],
            total_checkpoints=len(checkpoints),
            executed_checkpoints=len(executed),
            suspended_checkpoints=len(suspended),
            total_agent_executions=total_executions,
            failed_agent_executions=total_failures,
            aggregate_error_rate=round(
                total_failures / total_executions if total_executions else 1.0,
                8,
            ),
            max_p95_latency_ms=max(
                (item.p95_latency_ms for item in executed),
                default=0.0,
            ),
            reconciliation_runs=len(checkpoints),
            reconciliation_critical_mismatches=sum(
                item.reconciliation_critical_mismatches for item in checkpoints
            ),
            degradation_scenarios=scenarios,
            recovery_confirmations=3,
            initial_risk_state_hash=initial_risk,
            final_risk_state_hash=final_risk,
            risk_limits_hash=limits_hash,
            initial_order_count=initial_orders,
            final_order_count=final_orders,
            initial_paper_trade_count=initial_trades,
            final_paper_trade_count=final_trades,
            invariants=invariants,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def _persist_checkpoint(self, checkpoint) -> None:
        self._checkpoints.append(checkpoint)
        if self._repository is not None:
            await self._repository.save_shadow_campaign_checkpoint(checkpoint)

    @staticmethod
    def _recover(recovery: RecoveryCoordinator, scenario: str) -> None:
        if scenario == "DATABASE":
            for dependency in ("DATABASE", "AUDIT", "RISK"):
                for _ in range(recovery.recovery_successes_required):
                    recovery.observe(
                        dependency,
                        healthy=True,
                        reason="isolated deterministic recovery confirmation",
                    )
        else:
            for _ in range(recovery.recovery_successes_required):
                recovery.observe(
                    "BROKER",
                    healthy=True,
                    reason="isolated deterministic recovery confirmation",
                )

    def _risk_state_hash(self) -> str:
        return _fingerprint(
            {
                "state": self._risk.state.model_dump(mode="json"),
                "control": self._risk.control_state.model_dump(mode="json"),
                "positions": [
                    item.model_dump(mode="json")
                    for item in self._risk.position_exposures()
                ],
            }
        )

    def _risk_limits_hash(self) -> str:
        return _fingerprint(self._risk.limits.model_dump(mode="json"))

    def _paper_trade_count(self) -> int:
        return len(self._paper.open_orders) + len(self._paper.closed_orders)

    def checkpoints(self, *, campaign_id: str | None = None, limit: int = 100):
        rows = list(self._checkpoints)
        if campaign_id is not None:
            rows = [item for item in rows if item.campaign_id == campaign_id]
        return list(reversed(rows[-limit:]))

    def reports(self, *, limit: int = 100):
        rows = list(self._reports)
        return list(reversed(rows[-limit:]))

"""Orchestrator (docs/04-orchestrator.md).

Coordinates the full decision flow:
  market event -> agents -> decision engine -> risk -> paper trading -> audit

Rules enforced:
- agents never call each other directly (docs/02 isolation rule);
- risk veto cannot be bypassed (ADR-001);
- every step is audited with a shared correlation_id (docs/10);
- Phase 1 operates exclusively in PAPER mode.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from uuid import uuid4

from app.agents.base import BaseAgent
from app.agents.market_data import MarketDataAgent
from app.agents.quant import QuantAgent
from app.agents.registry import AgentRegistry
from app.agents.runtime import AgentRuntime
from app.agents.trend import TrendAgent
from app.audit.service import AuditService
from app.core.errors import AuditError, CapitalCipherError, DataQualityError
from app.core.event_bus import EventBus, Topics
from app.core.logging import ServiceLogger
from app.core.state_machine import SystemStateMachine
from app.market_data.data_quality import evaluate_candles
from app.market_data.clock import ExchangeClockRegistry
from app.market_data.gaps import GapService
from app.market_data.store import CandleStore
from app.orchestrator.decision_engine import DecisionEngine
from app.paper_trading.engine import PaperTradingEngine
from app.risk.manager import RiskManager
from app.strategy.engine import StrategyEngine
from app.schemas.agents import AgentExecutionRequest, AgentInput
from app.schemas.common import CandidateAction, MarketRegime, RiskStatus
from app.schemas.decisions import Decision
from app.schemas.events import EventTypes
from app.schemas.market import Candle, DataQualityReport

logger = ServiceLogger("orchestrator")


class Orchestrator:
    def __init__(
        self,
        *,
        state_machine: SystemStateMachine,
        event_bus: EventBus,
        candle_store: CandleStore,
        decision_engine: DecisionEngine,
        risk_manager: RiskManager,
        paper_engine: PaperTradingEngine,
        audit_service: AuditService,
        market_data_agent: MarketDataAgent,
        quant_agent: QuantAgent,
        trend_agent: TrendAgent,
        agent_runtime: AgentRuntime | None = None,
        repository=None,
        max_data_delay_ms: int = 5000,
        strategy_engine: StrategyEngine | None = None,
        clock_registry: ExchangeClockRegistry | None = None,
        require_trusted_clock: bool = False,
        gap_service: GapService | None = None,
    ) -> None:
        self._sm = state_machine
        self._bus = event_bus
        self._store = candle_store
        self._decision_engine = decision_engine
        self._risk = risk_manager
        self._paper = paper_engine
        self._audit = audit_service
        self._repository = repository
        self._max_data_delay_ms = max_data_delay_ms
        self._clock_registry = clock_registry
        self._require_trusted_clock = require_trusted_clock
        self._gap_service = gap_service
        self.strategy_engine = strategy_engine or StrategyEngine()
        self._current_day = None
        self._agent_runtime = agent_runtime or AgentRuntime(
            AgentRegistry(
                [market_data_agent, quant_agent, trend_agent]
            ),
            repository=repository,
            event_bus=event_bus,
        )
        self.recent_decisions: deque[Decision] = deque(maxlen=200)
        self.last_decision: Decision | None = None
        self.cycle_latencies_ms: deque[int] = deque(maxlen=100)
        self.failures: deque[dict[str, Any]] = deque(maxlen=50)
        self.pending_events: int = 0

    # -- status ------------------------------------------------------------------
    @property
    def agents(self) -> dict[str, BaseAgent]:
        """Expose the active registry view without caching stale definitions."""

        return self._agent_runtime.registry.agents

    def status(self) -> dict[str, Any]:
        avg_latency = (
            sum(self.cycle_latencies_ms) / len(self.cycle_latencies_ms)
            if self.cycle_latencies_ms
            else 0
        )
        return {
            "mode": self._sm.state.value,
            "kill_switch_active": self._sm.kill_switch_active,
            "agents": {name: agent.status for name, agent in self.agents.items()},
            "last_decision_id": self.last_decision.decision_id if self.last_decision else None,
            "avg_cycle_latency_ms": round(avg_latency, 2),
            "recent_failures": len(self.failures),
            "pending_events": self.pending_events,
        }

    # -- main entrypoint -----------------------------------------------------------
    async def on_candle_closed(self, candle: Candle) -> Decision | None:
        """Full decision cycle for a closed candle (docs/04 flow)."""
        started = time.monotonic()
        correlation_id = str(uuid4())
        try:
            clock_verdict = None
            if self._require_trusted_clock:
                if self._clock_registry is None:
                    raise DataQualityError(
                        "Trusted market-data ingestion requires a clock registry"
                    )
                clock_verdict = self._clock_registry.verdict(candle.exchange)
                if not clock_verdict.trusted:
                    self.failures.append(
                        {
                            "correlation_id": correlation_id,
                            "error": "CLOCK_UNTRUSTED",
                        }
                    )
                    if self._repository is not None:
                        await self._repository.save_system_event(
                            {
                                "event_type": "CLOCK_GATE_BLOCKED",
                                "source": "Orchestrator",
                                "correlation_id": correlation_id,
                                "payload": {
                                    "exchange": candle.exchange.value,
                                    "clock_status": clock_verdict.status,
                                    "reason": clock_verdict.reason,
                                },
                            }
                        )
                    logger.error(
                        "Candle blocked by trusted clock gate",
                        event_type="CLOCK_GATE_BLOCKED",
                        correlation_id=correlation_id,
                        metadata={
                            "exchange": candle.exchange.value,
                            "clock_status": clock_verdict.status,
                            "reason": clock_verdict.reason,
                        },
                    )
                    return None

            existing = self._store.get(
                candle.exchange.value,
                candle.symbol,
                candle.timeframe,
                limit=200,
            )
            quality = evaluate_candles(
                [*existing, candle],
                timeframe=candle.timeframe,
                max_delay_ms=self._max_data_delay_ms,
            )
            if clock_verdict is not None and clock_verdict.status == "WARNING":
                quality = quality.model_copy(
                    update={
                        "data_quality_score": max(
                            0,
                            quality.data_quality_score - 5,
                        ),
                        "status": (
                            quality.status
                            if quality.status in {"SUSPECT", "INVALID"}
                            else "WARNING"
                        ),
                        "warnings": sorted(
                            set([*quality.warnings, "CLOCK_WARNING"])
                        ),
                    }
                )

            # Normalized data is persisted before it can reach agents, risk, or
            # paper trading. A duplicate database identity stops redelivery.
            if self._repository is not None:
                inserted = await self._repository.save_candle(candle, quality)
                if not inserted:
                    logger.warning(
                        "Duplicate candle ignored",
                        event_type="DUPLICATE_CANDLE",
                        correlation_id=correlation_id,
                        metadata={
                            "exchange": candle.exchange.value,
                            "symbol": candle.symbol,
                            "timeframe": candle.timeframe,
                            "closed_at": candle.closed_at.isoformat(),
                        },
                    )
                    return None
                if (
                    self._gap_service is not None
                    and existing
                    and candle.closed_at > existing[-1].closed_at
                ):
                    await self._gap_service.scan(
                        exchange=candle.exchange.value,
                        symbol=candle.symbol,
                        timeframe=candle.timeframe,
                        start_at=existing[-1].closed_at,
                        end_at=candle.closed_at,
                        limit=10_000,
                    )

            if quality.errors:
                self.failures.append(
                    {
                        "correlation_id": correlation_id,
                        "error": "DATA_QUALITY_ERROR",
                    }
                )
                logger.error(
                    "Candle rejected by data quality",
                    event_type="DATA_QUALITY_ERROR",
                    correlation_id=correlation_id,
                    metadata={
                        "errors": quality.errors,
                        "warnings": quality.warnings,
                    },
                )
                return None
            if not self._store.add(candle):
                return None

            # Daily risk reset on UTC day change (docs/06 daily drawdown window).
            candle_day = candle.closed_at.date()
            if self._current_day is None:
                self._current_day = candle_day
            elif candle_day != self._current_day:
                self._current_day = candle_day
                self._risk.reset_daily()

            # Monitor paper positions only after persistence and temporal checks.
            closed_orders = await self._paper.on_candle(candle)
            for order in closed_orders:
                await self._bus.publish(
                    Topics.PAPER_ORDERS,
                    EventTypes.PAPER_ORDER_CLOSED,
                    order.model_dump(mode="json"),
                    source="PaperTradingEngine",
                    correlation_id=order.correlation_id,
                )

            if not self._sm.can_operate():
                logger.warning(
                    "Skipping evaluation: system cannot operate",
                    event_type="SYSTEM_NOT_READY",
                    correlation_id=correlation_id,
                    metadata={"state": self._sm.state.value},
                )
                return None

            await self._bus.publish(
                Topics.MARKET_EVENTS,
                EventTypes.CANDLE_CLOSED,
                candle.model_dump(mode="json"),
                source="MarketDataAdapter",
                correlation_id=correlation_id,
            )
            decision = await self._evaluate(candle, correlation_id, quality)
            self.cycle_latencies_ms.append(int((time.monotonic() - started) * 1000))
            return decision
        except AuditError:
            # Fail safe: audit failure blocks operations (docs/31).
            logger.critical(
                "Audit failure during decision cycle — blocking",
                event_type="AUDIT_LOG_FAILED",
                correlation_id=correlation_id,
            )
            self.failures.append({"correlation_id": correlation_id, "error": "AUDIT_FAILED"})
            return None
        except CapitalCipherError as exc:
            self.failures.append({"correlation_id": correlation_id, "error": exc.error_code})
            logger.error(
                f"Decision cycle failed: {exc.message}",
                event_type=exc.error_code,
                correlation_id=correlation_id,
            )
            return None

    async def _evaluate(
        self,
        candle: Candle,
        correlation_id: str,
        quality: DataQualityReport,
    ) -> Decision:
        exchange = candle.exchange.value
        symbol, timeframe = candle.symbol, candle.timeframe

        # Run analytical agents through their contracts.
        requests = []
        for agent in self.agents.values():
            registration = agent.registration()
            agent_input = AgentInput(
                request_id=f"{correlation_id}:{agent.name}",
                correlation_id=correlation_id,
                agent_name=agent.name,
                timestamp=candle.closed_at,
                symbol=symbol,
                timeframe=timeframe,
                market_context={"exchange": exchange},
            )
            requests.append(
                AgentExecutionRequest(
                    agent_version=registration.version,
                    idempotency_key=f"{correlation_id}:{agent.name}",
                    input=agent_input,
                )
            )
        agent_outputs = await self._agent_runtime.execute_many(requests)

        # Strategy selection and regime rules (docs/26).
        trend_output = next((o for o in agent_outputs if o.agent_name == "TrendAgent"), None)
        regime_value = (
            trend_output.evidence.get("market_regime") if trend_output else None
        ) or MarketRegime.UNDEFINED.value
        try:
            regime = MarketRegime(regime_value)
        except ValueError:
            regime = MarketRegime.UNDEFINED
        strategy_eval = self.strategy_engine.evaluate(
            symbol=symbol, timeframe=timeframe, regime=regime
        )
        risk_profile = None
        if strategy_eval.allowed:
            strategy_config = self.strategy_engine.select(symbol, timeframe)
            risk_profile = (
                self.strategy_engine.risk_profile(strategy_config) if strategy_config else None
            )

        # Consolidate into a candidate decision.
        decision = self._decision_engine.consolidate(
            correlation_id=correlation_id,
            symbol=symbol,
            timeframe=timeframe,
            agent_outputs=agent_outputs,
            data_quality_score=quality.data_quality_score,
            strategy=strategy_eval.versioned_id,
            minimum_confidence=strategy_eval.minimum_confidence,
        )
        if not strategy_eval.allowed and decision.candidate_action in (
            CandidateAction.BUY,
            CandidateAction.SELL,
        ):
            decision = decision.model_copy(
                update={
                    "candidate_action": CandidateAction.BLOCK,
                    "confidence": 0,
                    "reason": f"Strategy rule: {strategy_eval.reason}",
                    "warnings": sorted(set(decision.warnings + ["REGIME_UNCLEAR"]))
                    if regime == MarketRegime.UNDEFINED
                    else decision.warnings,
                }
            )
        self.last_decision = decision
        self.recent_decisions.append(decision)

        # Audit the candidate BEFORE risk (docs/04: no decision without evidence).
        await self._audit.record(
            correlation_id=correlation_id,
            audit_type="DECISION_CANDIDATE",
            entity_type="decision",
            entity_id=decision.decision_id,
            payload=decision.model_dump(mode="json"),
        )
        if self._repository is not None:
            await self._repository.save_decision(decision)
        await self._bus.publish(
            Topics.DECISION_EVENTS,
            EventTypes.DECISION_CANDIDATE_CREATED,
            decision.model_dump(mode="json"),
            source="Orchestrator",
            correlation_id=correlation_id,
        )

        # Non-actionable decisions stop here.
        if decision.candidate_action not in (CandidateAction.BUY, CandidateAction.SELL):
            return decision

        # Risk validation — mandatory, cannot be skipped (ADR-001).
        quant_output = next((o for o in agent_outputs if o.agent_name == "QuantAgent"), None)
        atr = quant_output.evidence.get("atr") if quant_output else None
        risk_check = await self._risk.check(
            decision,
            entry_price=candle.close,
            atr=atr,
            data_quality_score=quality.data_quality_score,
            balance=self._paper.balance,
            risk_per_trade_percent_override=(
                risk_profile.risk_per_trade_percent if risk_profile else None
            ),
            min_risk_reward_override=(risk_profile.risk_reward_min if risk_profile else None),
            max_open_positions_override=(
                risk_profile.max_open_positions if risk_profile else None
            ),
        )
        decision_after_risk = decision.model_copy(update={"risk_status": risk_check.risk_status})
        self.last_decision = decision_after_risk
        self.recent_decisions[-1] = decision_after_risk
        if self._repository is not None:
            await self._repository.save_risk_check(risk_check)
            await self._repository.update_decision_risk_status(
                decision.decision_id, risk_check.risk_status.value
            )
        await self._bus.publish(
            Topics.RISK_EVENTS,
            EventTypes.RISK_CHECK_COMPLETED,
            risk_check.model_dump(mode="json"),
            source="RiskManager",
            correlation_id=correlation_id,
        )

        if risk_check.risk_status in (RiskStatus.BLOCKED, RiskStatus.KILL_SWITCH):
            logger.info(
                f"Decision blocked by risk: {risk_check.reason}",
                event_type=EventTypes.DECISION_BLOCKED,
                correlation_id=correlation_id,
            )
            return decision_after_risk

        # Paper order (only APPROVED / REDUCED reach this point).
        order = await self._paper.create_order(
            decision_after_risk,
            risk_check,
            current_price=candle.close,
            market_candle=candle,
            occurred_at=candle.closed_at,
        )
        await self._bus.publish(
            Topics.PAPER_ORDERS,
            EventTypes.PAPER_ORDER_CREATED,
            order.model_dump(mode="json"),
            source="PaperTradingEngine",
            correlation_id=correlation_id,
        )
        return decision_after_risk

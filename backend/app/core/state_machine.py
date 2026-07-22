"""System state machine (docs/30-system-state-machine.md).

The system always operates in an explicit state. Transitions are controlled,
audited and safe. Phase 1 forbids any transition into LIVE_LOCKED or LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Awaitable

from app.core.errors import SecurityError, SystemStateError


class SystemState(str, Enum):
    OFFLINE = "OFFLINE"
    INITIALIZING = "INITIALIZING"
    PAPER = "PAPER"
    LIVE_LOCKED = "LIVE_LOCKED"
    LIVE = "LIVE"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    MAINTENANCE = "MAINTENANCE"


# docs/30 — allowed transitions for Phase 1 (future LIVE transitions excluded).
ALLOWED_TRANSITIONS: dict[SystemState, set[SystemState]] = {
    SystemState.OFFLINE: {SystemState.INITIALIZING},
    SystemState.INITIALIZING: {SystemState.PAPER, SystemState.ERROR},
    SystemState.PAPER: {SystemState.DEGRADED, SystemState.ERROR, SystemState.OFFLINE},
    SystemState.DEGRADED: {SystemState.PAPER, SystemState.ERROR},
    SystemState.ERROR: {SystemState.MAINTENANCE},
    SystemState.MAINTENANCE: {SystemState.OFFLINE},
    SystemState.LIVE_LOCKED: set(),
    SystemState.LIVE: set(),
}

PHASE_1_FORBIDDEN_TARGETS = {SystemState.LIVE, SystemState.LIVE_LOCKED}

# States in which new decisions / paper orders are allowed.
OPERATIONAL_STATES = {SystemState.PAPER, SystemState.DEGRADED}


@dataclass
class StateTransition:
    previous: SystemState
    new: SystemState
    reason: str
    actor: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str | None = None


TransitionListener = Callable[[StateTransition], Awaitable[None]]


class SystemStateMachine:
    """Holds the explicit operational state of the whole system."""

    def __init__(self, initial: SystemState = SystemState.OFFLINE) -> None:
        self._state = initial
        self._kill_switch_active = False
        self._kill_switch_reason: str | None = None
        self._history: list[StateTransition] = []
        self._listeners: list[TransitionListener] = []

    @property
    def state(self) -> SystemState:
        return self._state

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def kill_switch_reason(self) -> str | None:
        return self._kill_switch_reason

    @property
    def history(self) -> list[StateTransition]:
        return list(self._history)

    def add_listener(self, listener: TransitionListener) -> None:
        self._listeners.append(listener)

    def can_operate(self) -> bool:
        """Whether new decisions and paper orders may be produced."""
        return self._state in OPERATIONAL_STATES and not self._kill_switch_active

    async def transition(
        self,
        new_state: SystemState,
        *,
        reason: str,
        actor: str,
        correlation_id: str | None = None,
    ) -> StateTransition:
        if new_state in PHASE_1_FORBIDDEN_TARGETS:
            raise SecurityError(
                f"Transition to {new_state.value} is prohibited in Phase 1.",
                correlation_id=correlation_id,
            )
        if new_state == self._state:
            raise SystemStateError(
                f"System already in state {new_state.value}.", correlation_id=correlation_id
            )
        allowed = ALLOWED_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise SystemStateError(
                f"Transition {self._state.value} -> {new_state.value} is not allowed.",
                correlation_id=correlation_id,
            )
        transition = StateTransition(
            previous=self._state,
            new=new_state,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        )
        self._state = new_state
        self._history.append(transition)
        for listener in self._listeners:
            await listener(transition)
        return transition

    async def trigger_kill_switch(
        self, *, reason: str, actor: str, correlation_id: str | None = None
    ) -> StateTransition | None:
        """Kill switch forces operational states into ERROR (docs/30)."""
        self._kill_switch_active = True
        self._kill_switch_reason = reason
        if self._state in OPERATIONAL_STATES:
            return await self.transition(
                SystemState.ERROR,
                reason=f"KILL_SWITCH_TRIGGERED: {reason}",
                actor=actor,
                correlation_id=correlation_id,
            )
        return None

    def reset_kill_switch_after_maintenance(self) -> None:
        """Kill switch can only be cleared in an explicit maintenance window."""
        if self._state != SystemState.MAINTENANCE:
            raise SystemStateError(
                "Kill switch reset requires MAINTENANCE state."
            )
        self._kill_switch_active = False
        self._kill_switch_reason = None

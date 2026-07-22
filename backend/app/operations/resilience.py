"""Dependency health, safe degradation and deterministic recovery gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DependencyName = Literal[
    "DATABASE",
    "AUDIT",
    "RISK",
    "BROKER",
    "MARKET_DATA",
    "SHADOW_RUNTIME",
]
RecoveryMode = Literal["HEALTHY", "DEGRADED", "SAFE_HALT"]


@dataclass
class DependencyState:
    name: DependencyName
    critical: bool
    healthy: bool = True
    reason: str = "healthy"
    consecutive_successes: int = 0
    consecutive_failures: int = 0


class RecoveryCoordinator:
    """Fails closed on critical dependencies and recovers conservatively."""

    def __init__(self, *, recovery_successes_required: int = 3) -> None:
        if not 2 <= recovery_successes_required <= 100:
            raise ValueError("Recovery confirmation must be 2..100")
        self._required = recovery_successes_required
        self._dependencies: dict[DependencyName, DependencyState] = {
            name: DependencyState(name=name, critical=critical)
            for name, critical in (
                ("DATABASE", True),
                ("AUDIT", True),
                ("RISK", True),
                ("BROKER", False),
                ("MARKET_DATA", False),
                ("SHADOW_RUNTIME", False),
            )
        }
        self._critical_latched = False

    @property
    def recovery_successes_required(self) -> int:
        return self._required

    def observe(
        self,
        name: DependencyName,
        *,
        healthy: bool,
        reason: str,
    ) -> DependencyState:
        state = self._dependencies[name]
        state.healthy = healthy
        state.reason = reason[:500]
        if healthy:
            state.consecutive_successes += 1
            state.consecutive_failures = 0
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            if state.critical:
                self._critical_latched = True
        if self._critical_latched and all(
            item.healthy
            and item.consecutive_successes >= self._required
            for item in self._dependencies.values()
            if item.critical
        ):
            self._critical_latched = False
        return DependencyState(**state.__dict__)

    @property
    def mode(self) -> RecoveryMode:
        if self._critical_latched or any(
            not state.healthy
            for state in self._dependencies.values()
            if state.critical
        ):
            return "SAFE_HALT"
        if any(
            not state.healthy
            for state in self._dependencies.values()
            if not state.critical
        ):
            return "DEGRADED"
        return "HEALTHY"

    @property
    def decisions_allowed(self) -> bool:
        return self.mode != "SAFE_HALT"

    @property
    def shadow_allowed(self) -> bool:
        return self.mode == "HEALTHY"

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "decisions_allowed": self.decisions_allowed,
            "shadow_allowed": self.shadow_allowed,
            "recovery_successes_required": self.recovery_successes_required,
            "dependencies": {
                name: {
                    "critical": state.critical,
                    "healthy": state.healthy,
                    "reason": state.reason,
                    "consecutive_successes": state.consecutive_successes,
                    "consecutive_failures": state.consecutive_failures,
                }
                for name, state in self._dependencies.items()
            },
        }

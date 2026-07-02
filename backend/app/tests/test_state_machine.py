"""State machine tests (docs/30) and Phase 1 security guarantees."""

from __future__ import annotations

import pytest

from app.core.errors import SecurityError, SystemStateError
from app.core.state_machine import SystemState, SystemStateMachine


async def test_boot_sequence():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="test")
    await sm.transition(SystemState.PAPER, reason="ready", actor="test")
    assert sm.state == SystemState.PAPER
    assert sm.can_operate()


async def test_live_transition_is_security_error():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="test")
    await sm.transition(SystemState.PAPER, reason="ready", actor="test")
    with pytest.raises(SecurityError):
        await sm.transition(SystemState.LIVE, reason="attempt", actor="test")
    with pytest.raises(SecurityError):
        await sm.transition(SystemState.LIVE_LOCKED, reason="attempt", actor="test")


async def test_invalid_transition_rejected():
    sm = SystemStateMachine()
    with pytest.raises(SystemStateError):
        await sm.transition(SystemState.PAPER, reason="skip init", actor="test")


async def test_error_cannot_return_directly_to_paper():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="test")
    await sm.transition(SystemState.ERROR, reason="failure", actor="test")
    with pytest.raises(SystemStateError):
        await sm.transition(SystemState.PAPER, reason="shortcut", actor="test")
    # Correct recovery path: ERROR -> MAINTENANCE -> OFFLINE -> INITIALIZING -> PAPER
    await sm.transition(SystemState.MAINTENANCE, reason="recover", actor="test")
    await sm.transition(SystemState.OFFLINE, reason="recover", actor="test")
    await sm.transition(SystemState.INITIALIZING, reason="recover", actor="test")
    await sm.transition(SystemState.PAPER, reason="recover", actor="test")
    assert sm.state == SystemState.PAPER


async def test_kill_switch_forces_error_and_blocks_operation():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="test")
    await sm.transition(SystemState.PAPER, reason="ready", actor="test")
    await sm.trigger_kill_switch(reason="emergency", actor="test")
    assert sm.state == SystemState.ERROR
    assert sm.kill_switch_active
    assert not sm.can_operate()


async def test_kill_switch_cannot_reset_while_operational():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="test")
    await sm.transition(SystemState.PAPER, reason="ready", actor="test")
    sm._kill_switch_active = True
    with pytest.raises(SystemStateError):
        sm.reset_kill_switch_after_maintenance()


async def test_transitions_are_audited_in_history():
    sm = SystemStateMachine()
    await sm.transition(SystemState.INITIALIZING, reason="boot", actor="tester")
    assert len(sm.history) == 1
    t = sm.history[0]
    assert t.previous == SystemState.OFFLINE
    assert t.new == SystemState.INITIALIZING
    assert t.actor == "tester"
    assert t.reason == "boot"

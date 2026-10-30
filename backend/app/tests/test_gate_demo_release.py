"""Real risk/OMS integration with a simulated venue; no real canary claims."""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import SecurityError
from app.demo.gate_cycle_journal import GateCycleJournal
from app.execution.gate_demo_release import GateDemoOwnerGrant, GateDemoRelease
from app.tests.test_gate_oms_connection import flow, analysis, NOW
from app.tests.test_gate_testnet_execution import order

REV = "f" * 40
BINDING = "c" * 64


def grant(**changes):
    now = datetime.fromtimestamp(NOW, timezone.utc)
    fields = dict(
        protocol="gate-demo-owner-v1",
        grant_id=str(uuid4()),
        owner_id="ernanefideles22-bot",
        authorization_reference="Owner requested Gate demo corrections",
        source_revision=REV,
        technical_evidence_sha256="d" * 64,
        environment="GATE_TESTNET",
        acknowledgement="GATE_TESTNET_VIRTUAL_FUNDS_ONLY",
        mode="CANARY_ONLY",
        issued_at=now - timedelta(seconds=10),
        expires_at=now + timedelta(hours=1),
    )
    fields.update(changes)
    return GateDemoOwnerGrant(**fields)


def policy(journal, authorization=None, **kwargs):
    return GateDemoRelease(
        journal,
        authorization or grant(),
        source_revision=REV,
        account_binding=BINDING,
        clock=lambda: NOW,
        **kwargs
    )


def wire(connection, authorization=None):
    release = policy(connection.journal, authorization)
    connection.demo_release = release
    connection.release_guard = release.authorized
    connection.oms._release_guard = release.authorized
    connection.adapter._entry_order_guard = release.authorize_order
    return release


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "GATE_LIVE"},
        {"acknowledgement": ""},
        {"mode": "AGENTS"},
        {"mode": "CANARY_ONLY", "accepted_canary_sha256": "a" * 64},
        {"grant_id": "not-a-uuid"},
        {"expires_at": datetime.fromtimestamp(NOW, timezone.utc) + timedelta(days=2)},
        {"source_revision": "main"},
        {"unexpected_override": True},
    ],
)
def test_owner_grant_rejects_unsafe_or_ambiguous_scope(changes):
    with pytest.raises((ValidationError, ValueError)):
        grant(**changes)


def test_grant_json_and_exact_revision(tmp_path):
    journal = GateCycleJournal(tmp_path / "cycles.db")
    try:
        authorization = grant()
        parsed = GateDemoOwnerGrant.model_validate_json(authorization.model_dump_json())
        release = policy(journal, parsed)
        assert release.current()
        with pytest.raises(SecurityError):
            GateDemoRelease(
                journal, parsed, source_revision="e" * 40, account_binding=BINDING
            )
        with pytest.raises(SecurityError):
            GateDemoRelease(
                journal, parsed, source_revision=REV, account_binding="b" * 64
            )
        with pytest.raises(sqlite3.IntegrityError):
            journal.db.execute("DELETE FROM gate_demo_grants")
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_expiry_and_missing_real_canary_block_agents(tmp_path):
    journal = GateCycleJournal(tmp_path / "cycles.db")
    try:
        expired = grant(
            issued_at=datetime.fromtimestamp(NOW - 20, timezone.utc),
            expires_at=datetime.fromtimestamp(NOW - 1, timezone.utc),
        )
        assert not await policy(journal, expired).authorized()
        agents = policy(journal, grant(mode="AGENTS", accepted_canary_sha256="0" * 64))
        assert not await agents.authorized()
        assert not agents.may_queue()
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_order_specific_reservation_survives_restart_and_cannot_be_reset(
    tmp_path,
):
    path = tmp_path / "cycles.db"
    journal = GateCycleJournal(path)
    authorization = grant()
    release = policy(journal, authorization)
    release.reserve(order())
    await release.authorize_order(order())
    with pytest.raises(SecurityError):
        await release.authorize_order(order(oms_order_id="another-order"))
    assert not release.may_queue()
    journal.close()
    journal = GateCycleJournal(path)
    try:
        release = policy(journal, authorization)
        assert release.slot() == "order-0001"
        release.reserve(order())  # same command only, no new budget
        replacement = policy(journal, grant())
        assert not replacement.may_queue()
        with pytest.raises(SecurityError):
            replacement.reserve(order(oms_order_id="another-order"))
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_owner_canary_uses_real_risk_and_requires_separate_acceptance(flow):
    connection, venue, _, _ = flow
    release = wire(connection)
    assert (await connection.process_analysis(analysis("WAIT")))[
        "status"
    ] == "WAITING_FOR_SIGNAL"
    assert release.slot() is None
    result = await connection.process_analysis(analysis())
    assert result["status"] == "QUEUED"
    assert release.slot() == result["oms_order_id"] and not venue.writes
    await connection.tick()
    assert venue.writes == ["entry", "stop"]
    await connection.tick()
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]
    assert connection.status == "AWAITING_OWNER_CANARY_ACCEPTANCE"
    sha = release.status()["canary_evidence_sha256"]
    assert sha and not release.accepted()
    assert not connection.risk.position_exposures()
    assert (await connection.process_analysis(analysis()))[
        "status"
    ] == "CANARY_BUDGET_CONSUMED"
    await connection.tick()
    assert len(venue.writes) == 4
    accepted = grant(mode="AGENTS", accepted_canary_sha256=sha)
    release = wire(connection, accepted)
    assert await release.authorized()
    assert release.accepted()
    assert (await connection.process_analysis(analysis()))[
        "status"
    ] == "ALREADY_EVALUATED"
    different_evidence = policy(
        connection.journal,
        grant(
            mode="AGENTS",
            accepted_canary_sha256=sha,
            technical_evidence_sha256="a" * 64,
        ),
    )
    assert not await different_evidence.authorized()


@pytest.mark.asyncio
async def test_failed_stop_never_produces_approval_evidence(flow):
    connection, venue, _, _ = flow
    release = wire(connection)
    await connection.process_analysis(analysis())
    venue.failure = "stop"
    with pytest.raises(SecurityError):
        await connection.tick()
    assert release.status()["canary_evidence_sha256"] is None
    assert connection.journal.recovery_status()["recovery_required"]
    assert not release.may_queue()


@pytest.mark.asyncio
async def test_canary_reservation_blocks_foreign_queued_order_before_any_write(flow):
    connection, venue, _, _ = flow
    release = wire(connection)
    await connection.process_analysis(analysis())
    with pytest.raises(SecurityError):
        await connection.adapter.submit_order(order(oms_order_id="unrelated-order"))
    assert not venue.writes


@pytest.mark.asyncio
async def test_unproven_close_or_stale_protection_cannot_be_accepted(flow):
    connection, _, _, _ = flow
    release = wire(connection)
    result = await connection.process_analysis(analysis())
    await connection.tick()
    stored = await connection.repository.load_oms_order(result["oms_order_id"])
    evidence = await connection.adapter.capture_canary_protection(stored)
    evidence["stop_observed_at"] = NOW - 10
    with pytest.raises(SecurityError):
        release.record_protected(stored, evidence)
    with pytest.raises(SecurityError):
        release.record_closed(
            stored, dict(state="CLOSED", order_id=stored.oms_order_id)
        )


def test_gate_shadow_cli_has_no_network_and_keeps_all_300_agents():
    backend = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-B", "scripts/run_month11_shadow_validation.py"],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert payload["network_attempt_count"] == 0
    assert payload["external_evidence_enabled"] is False
    assert payload["real_exchange_execution_proven"] is False
    assert payload["market_provenance"] == "GATE_SYNTHETIC_REPLAY"
    report = payload["report"]
    assert report["status"] == "PASSED"
    assert report["total_agent_executions"] == 1800
    assert report["failed_agent_executions"] == 0
    assert report["initial_order_count"] == report["final_order_count"] == 0


@pytest.mark.asyncio
async def test_expired_canary_still_closes_owned_position_without_new_entry(flow):
    connection, venue, _, _ = flow
    release = wire(
        connection, grant(expires_at=datetime.fromtimestamp(NOW + 1, timezone.utc))
    )
    await connection.process_analysis(analysis())
    await connection.tick()
    release.clock = lambda: NOW + 2
    connection.clock = lambda: NOW + 2
    connection.adapter._clock = lambda: NOW + 2
    await connection.tick()
    assert venue.writes == ["entry", "stop", "close", "cancel_stop"]
    assert not await release.authorized()
    assert not release.may_queue()


@pytest.mark.asyncio
async def test_zero_fill_consumes_canary_without_claiming_stop_or_exit_proof(flow):
    connection, venue, _, _ = flow
    release = wire(connection)
    entry = venue.place_entry
    rows = venue.rows

    async def zero_fill(**kwargs):
        ack = await entry(**kwargs)
        venue.orders["11"].update(left=abs(venue.orders["11"]["size"]), finish_as="ioc")
        venue.size = 0
        return ack

    async def zero_fills(kind, **kwargs):
        return [] if kind == "fills" else await rows(kind, **kwargs)

    venue.place_entry, venue.rows = zero_fill, zero_fills
    await connection.process_analysis(analysis())
    await connection.tick()
    await connection.tick()
    assert venue.writes == ["entry"]
    assert connection.status == "CANARY_UNFILLED"
    assert not release.may_queue()
    assert release.status()["canary_evidence_sha256"] is None


@pytest.mark.asyncio
async def test_measured_canary_report_is_immutable_and_bound_to_revision(flow):
    connection, _, _, _ = flow
    release = wire(connection)
    await connection.process_analysis(analysis())
    await connection.tick()
    await connection.tick()
    sha = release.status()["canary_evidence_sha256"]
    with pytest.raises(sqlite3.IntegrityError):
        release.db.execute("DELETE FROM gate_demo_canary_evidence")
    changed = grant(mode="AGENTS", accepted_canary_sha256=sha, source_revision="e" * 40)
    other = GateDemoRelease(
        connection.journal,
        changed,
        source_revision="e" * 40,
        account_binding=BINDING,
        clock=lambda: NOW,
    )
    assert not await other.authorized()
    changed = grant(
        mode="AGENTS",
        accepted_canary_sha256=sha,
        expires_at=datetime.fromtimestamp(NOW - 1, timezone.utc),
        issued_at=datetime.fromtimestamp(NOW - 10, timezone.utc),
    )
    assert not await policy(connection.journal, changed).authorized()


@pytest.mark.asyncio
async def test_public_status_does_not_label_canary_as_automatic_agents(flow, tmp_path):
    from contextlib import asynccontextmanager
    from httpx import ASGITransport, AsyncClient
    from app.demo.gate_service import create_app

    connection, _, _, _ = flow
    wire(connection)
    connection.status = "CANARY_VALIDATING"

    async def no_worker(stop):
        await stop.wait()

    connection.run = no_worker

    @asynccontextmanager
    async def connector(*args, **kwargs):
        yield connection

    async def market(*args, **kwargs):
        return [{}] * 60

    app = create_app(
        environment={"GATE_OMS_CONNECTION_ENABLED": "true"},
        storage_path=str(tmp_path),
        mount_check=lambda _: True,
        oms_connector=connector,
        market_fetch=market,
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = (await client.get("/api/gate/status")).json()
            assert payload["canary_execution_enabled"] is True
            assert payload["execution_enabled"] is False
            assert payload["demo_release"]["mode"] == "CANARY_ONLY"
            assert "authorization_reference" not in json.dumps(payload)

"""Owner-controlled Gate virtual-funds releases, separate from external audits.

The trusted input is server configuration controlled by the project owner. It
is not an independent attestation. One canary reservation survives restarts;
agent mode requires the owner's acceptance of a measured, matching canary.
"""

import hashlib
import json
import re
import time
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.core.errors import SecurityError
from app.demo.gate_entry_evidence import confirmed_entry
from app.demo.gate_exit_evidence import confirmed_exit
from app.demo.gate_stop_validation import stop_matches

ACK = "GATE_TESTNET_VIRTUAL_FUNDS_ONLY"
TECHNICAL_CANARY_STRATEGY = "GATE_TECHNICAL_CANARY"


def digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class GateDemoOwnerGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Literal["gate-demo-owner-v1"]
    grant_id: str
    owner_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
    authorization_reference: str = Field(min_length=10, max_length=500)
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    technical_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment: Literal["GATE_TESTNET"]
    acknowledgement: Literal["GATE_TESTNET_VIRTUAL_FUNDS_ONLY"]
    mode: Literal["CANARY_ONLY", "AGENTS"]
    canary_trigger: Literal["AGENT_SIGNAL", "TECHNICAL_PROBE"] = "AGENT_SIGNAL"
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    accepted_canary_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bounded(self):
        UUID(self.grant_id)
        lifetime = (self.expires_at - self.issued_at).total_seconds()
        if not 0 < lifetime <= 86400:
            raise ValueError("Gate demo owner grant is valid for at most 24 hours")
        if (self.mode == "AGENTS") != (self.accepted_canary_sha256 is not None):
            raise ValueError("Only AGENTS mode requires an accepted real canary digest")
        if self.mode == "AGENTS" and self.canary_trigger != "AGENT_SIGNAL":
            raise ValueError("Technical probes only belong to the canary stage")
        return self


class GateDemoRelease:
    def __init__(
        self, journal, grant, *, source_revision, account_binding, clock=time.time
    ):
        if not isinstance(grant, GateDemoOwnerGrant):
            raise SecurityError("Validated Gate owner grant required")
        if grant.source_revision != source_revision or not re.fullmatch(
            r"[a-f0-9]{64}", account_binding
        ):
            raise SecurityError("Gate owner grant revision or account mismatch")
        self.journal, self.grant, self.binding, self.clock = (
            journal,
            grant,
            account_binding,
            clock,
        )
        self.db = journal.db
        tables = {
            "gate_demo_grants": "id TEXT PRIMARY KEY, payload TEXT NOT NULL",
            "gate_demo_canary_slots": "grant_id TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL",
            "gate_demo_protected_observations": "order_id TEXT PRIMARY KEY, payload TEXT NOT NULL",
            "gate_demo_canary_evidence": "sha256 TEXT PRIMARY KEY, order_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL",
        }
        for name, columns in tables.items():
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {name} ({columns})")
            for operation in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {name}_deny_{operation.lower()} "
                    f"BEFORE {operation} ON {name} BEGIN SELECT RAISE(ABORT,'immutable Gate demo evidence'); END"
                )
        payload = json.dumps(
            {
                # Preserve the exact representation of grants written before
                # canary_trigger existed, including on a service restart.
                "grant": grant.model_dump(
                    mode="json",
                    exclude=(
                        {"canary_trigger"}
                        if "canary_trigger" not in grant.model_fields_set
                        else set()
                    ),
                ),
                "account_binding": account_binding,
            },
            sort_keys=True,
        )
        prior = self.db.execute(
            "SELECT payload FROM gate_demo_grants WHERE id=?", (grant.grant_id,)
        ).fetchone()
        if prior and prior[0] != payload:
            raise SecurityError("Gate demo grant identity cannot be reused")
        self.db.execute(
            "INSERT OR IGNORE INTO gate_demo_grants VALUES (?,?)",
            (grant.grant_id, payload),
        )

    def current(self):
        return (
            self.grant.issued_at.timestamp()
            <= self.clock()
            < self.grant.expires_at.timestamp()
        )

    def slot(self):
        row = self.db.execute(
            "SELECT order_id FROM gate_demo_canary_slots WHERE grant_id=?",
            (self.grant.grant_id,),
        ).fetchone()
        return row[0] if row else None

    def accepted(self):
        if self.grant.mode != "AGENTS":
            return False
        row = self.db.execute(
            "SELECT payload FROM gate_demo_canary_evidence WHERE sha256=?",
            (self.grant.accepted_canary_sha256,),
        ).fetchone()
        if not row:
            return False
        payload = json.loads(row[0])
        return (
            digest(payload) == self.grant.accepted_canary_sha256
            and payload["source_revision"] == self.grant.source_revision
            and payload["account_binding"] == self.binding
            and payload["technical_evidence_sha256"]
            == self.grant.technical_evidence_sha256
            and payload["environment"] == "GATE_TESTNET"
            and payload["status"] == "VERIFIED"
        )

    async def authorized(self):
        # A reserved canary remains dispatchable only as that same order; the
        # order-specific guard below prevents another queued command from using it.
        return self.current() and (self.grant.mode == "CANARY_ONLY" or self.accepted())

    def previously_consumed(self):
        rows = self.db.execute(
            "SELECT g.payload FROM gate_demo_canary_slots s JOIN gate_demo_grants g ON g.id=s.grant_id"
        ).fetchall()
        return any(
            (item := json.loads(row[0]))["account_binding"] == self.binding
            and item["grant"]["source_revision"] == self.grant.source_revision
            for row in rows
        )

    def may_queue(self):
        return self.current() and (
            self.grant.mode == "AGENTS"
            and self.accepted()
            or self.grant.mode == "CANARY_ONLY"
            and not self.previously_consumed()
        )

    def reserve(self, order):
        self.validate_order(order)
        if self.grant.mode != "CANARY_ONLY":
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.slot()
            if prior is not None and prior != order.oms_order_id:
                raise SecurityError("Gate owner canary budget already consumed")
            # A new grant must not reset a previously consumed canary budget for
            # this revision/account, even if the previous canary failed or had no fill.
            previous = self.db.execute(
                "SELECT g.payload FROM gate_demo_canary_slots s JOIN gate_demo_grants g ON g.id=s.grant_id WHERE s.grant_id != ?",
                (self.grant.grant_id,),
            ).fetchall()
            for (payload,) in previous:
                item = json.loads(payload)
                if (
                    item["account_binding"] == self.binding
                    and item["grant"]["source_revision"] == self.grant.source_revision
                ):
                    raise SecurityError(
                        "This revision already consumed its Gate canary"
                    )
            self.db.execute(
                "INSERT OR IGNORE INTO gate_demo_canary_slots VALUES (?,?)",
                (self.grant.grant_id, order.oms_order_id),
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def validate_order(self, order):
        if (
            not self.current()
            or order.exchange.value != "GATE"
            or order.environment.value != "TESTNET"
            or order.symbol != "BTCUSDT"
            or order.timeframe != "15m"
            or order.leverage != 1
            or not 0 < order.requested_notional <= 50
            or order.order_type.value != "MARKET"
            or order.time_in_force.value != "IOC"
            or (order.strategy == TECHNICAL_CANARY_STRATEGY)
            != (self.grant.canary_trigger == "TECHNICAL_PROBE")
        ):
            raise SecurityError("Gate owner release order exceeds its scope")

    async def authorize_order(self, order):
        self.validate_order(order)
        if not await self.authorized():
            raise SecurityError("Gate owner release unavailable")
        if self.grant.mode == "CANARY_ONLY" and self.slot() != order.oms_order_id:
            raise SecurityError("Gate queued order does not own the reserved canary")

    def record_protected(self, order, payload):
        if self.grant.mode != "CANARY_ONLY" or self.slot() != order.oms_order_id:
            raise SecurityError("Gate canary protection has no reserved owner")
        row = self.journal.read(order.oms_order_id)
        entry_identity = self.db.execute(
            "SELECT client_id,order_id FROM entry_identity WHERE cycle_id=?",
            (order.oms_order_id,),
        ).fetchone()
        if (
            payload.get("state") != "PROTECTED"
            or payload.get("order_id") != order.oms_order_id
            or row["state"] != "PROTECTED"
            or not entry_identity
            or payload["entry_order_id"] != entry_identity[1]
            or entry_identity[0] != order.client_order_id
            or payload["stop_id"] != row["stop_id"]
            or confirmed_entry(
                order=payload["entry"],
                position=payload["position"],
                contract=row["contract"],
                order_id=entry_identity[1],
                client_id=entry_identity[0],
                planned_size=row["planned"],
                order_observed_at=payload["entry_observed_at"],
                position_observed_at=payload["position_observed_at"],
                now=self.clock(),
            )
            is None
            or not stop_matches(
                payload["stop"],
                order_id=row["stop_id"],
                contract=row["contract"],
                position_size=row["position"],
                trigger_price=row["target"],
                observed_at=payload["stop_observed_at"],
                now=self.clock(),
            )
        ):
            raise SecurityError("Gate canary protection observation invalid")
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        self.db.execute(
            "INSERT OR IGNORE INTO gate_demo_protected_observations VALUES (?,?)",
            (order.oms_order_id, encoded),
        )

    def record_closed(self, order, payload):
        row = self.db.execute(
            "SELECT payload FROM gate_demo_protected_observations WHERE order_id=?",
            (order.oms_order_id,),
        ).fetchone()
        if (
            self.grant.mode != "CANARY_ONLY"
            or self.slot() != order.oms_order_id
            or not row
            or payload.get("state") != "CLOSED"
            or payload.get("order_id") != order.oms_order_id
            or self.journal.read(order.oms_order_id)["state"] != "CLOSED"
            or self.journal.recovery_status()["recovery_required"]
            or payload.get("reconciliation_status") != "MATCHED"
            or not payload.get("reconciliation_run_id")
            or payload.get("risk_clear") is not True
        ):
            raise SecurityError("Gate canary entry, protection and close not proven")
        protected = json.loads(row[0])
        if (
            payload["entry_order_id"] != protected["entry_order_id"]
            or payload["stop_id"] != protected["stop_id"]
        ):
            raise SecurityError("Gate canary identities changed")
        identity = self.db.execute(
            "SELECT client_id,order_id FROM exit_identity WHERE cycle_id=?",
            (order.oms_order_id,),
        ).fetchone()
        if not identity or not confirmed_exit(
            order=payload["exit"],
            position=payload["position"],
            inventory=payload["inventory"],
            contract="BTC_USDT",
            order_id=identity[1],
            client_id=identity[0],
            order_observed_at=payload["exit_observed_at"],
            position_observed_at=payload["position_observed_at"],
            inventory_started_at=payload["inventory_started_at"],
            inventory_finished_at=payload["inventory_finished_at"],
            now=self.clock(),
            expected_signed_position=protected['position']['size'],
        ):
            raise SecurityError(
                "Gate canary owned reduce-only exit or flat inventory not proven"
            )
        report = {
            "protocol": "gate-demo-canary-evidence-v1",
            "status": "VERIFIED",
            "environment": "GATE_TESTNET",
            "source_revision": self.grant.source_revision,
            "account_binding": self.binding,
            "grant_id": self.grant.grant_id,
            "technical_evidence_sha256": self.grant.technical_evidence_sha256,
            "order_id": order.oms_order_id,
            "canary_trigger": self.grant.canary_trigger,
            "strategy": order.strategy,
            "strategy_performance_sample": False,
            "limits": {"max_notional_usdt": 50, "max_positions": 1, "leverage": 1},
            "protected": protected,
            "closed": payload,
        }
        sha = digest(report)
        prior = self.db.execute(
            "SELECT sha256 FROM gate_demo_canary_evidence WHERE order_id=?",
            (order.oms_order_id,),
        ).fetchone()
        if prior:
            return prior[0]
        self.db.execute(
            "INSERT INTO gate_demo_canary_evidence VALUES (?,?,?)",
            (sha, order.oms_order_id, json.dumps(report, sort_keys=True)),
        )
        return sha

    def status(self):
        row = self.db.execute(
            "SELECT sha256 FROM gate_demo_canary_evidence WHERE order_id=?",
            (self.slot(),),
        ).fetchone()
        return {
            "policy": "OWNER_GATE_DEMO",
            "mode": self.grant.mode,
            "canary_trigger": self.grant.canary_trigger,
            "grant_current": self.current(),
            "canary_reserved": self.slot() is not None,
            "canary_evidence_sha256": row[0] if row else None,
            "canary_accepted": self.accepted(),
        }

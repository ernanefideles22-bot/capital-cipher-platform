"""Prepare an owner grant for review. No network, database or configuration write."""

import argparse
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.execution.gate_demo_release import GateDemoOwnerGrant, ACK


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--technical-evidence-sha256", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--hours", type=float, default=1)
    parser.add_argument(
        "--mode", choices=["CANARY_ONLY", "AGENTS"], default="CANARY_ONLY"
    )
    parser.add_argument("--accepted-canary-sha256")
    parser.add_argument(
        "--canary-trigger",
        choices=["AGENT_SIGNAL", "TECHNICAL_PROBE"],
        default="AGENT_SIGNAL",
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    grant = GateDemoOwnerGrant(
        protocol="gate-demo-owner-v1",
        grant_id=str(uuid4()),
        owner_id=args.owner_id,
        authorization_reference=args.authorization_reference,
        source_revision=args.source_revision,
        technical_evidence_sha256=args.technical_evidence_sha256,
        environment="GATE_TESTNET",
        acknowledgement=ACK,
        mode=args.mode,
        canary_trigger=args.canary_trigger,
        issued_at=now,
        expires_at=now + timedelta(hours=args.hours),
        accepted_canary_sha256=args.accepted_canary_sha256,
    )
    print(grant.model_dump_json())


if __name__ == "__main__":
    main()

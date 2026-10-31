"""Single-run, bounded Railway PAPER E1 audit. Never starts an API or broker.

This command is safe to stage on an idle *existing diagnostic service*.
It does not call the OMS, adapters, database migrations, or retention workers.
The database's identity is checked before the report opens a READ ONLY SQL
transaction. A dedicated SELECT-only role is preferred when provisioning.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
from urllib.parse import urlsplit
import re

from scripts.paper_e1_readonly_report import export_forensics, read_only_url


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"true", "1", "yes", "on"}


def verify_environment(env: dict[str, str]) -> str:
    """Refuse wrong environment and any database endpoint not explicitly expected."""
    if env.get("APP_ENV") != "staging":
        raise RuntimeError("E1_NOT_STAGING")
    if env.get("RAILWAY_ENVIRONMENT_NAME") not in {"staging"}:
        raise RuntimeError("E1_WRONG_RAILWAY_ENVIRONMENT")
    if env.get("SYSTEM_MODE") != "PAPER":
        raise RuntimeError("E1_NOT_PAPER")
    if env.get("OMS_EXECUTION_ENVIRONMENT") != "PAPER":
        raise RuntimeError("E1_OMS_ENVIRONMENT_NOT_PAPER")
    if _enabled(env.get("OMS_TESTNET_ENABLED")):
        raise RuntimeError("E1_TESTNET_ENABLED")
    actual = env.get("DATABASE_URL") or ""
    if not actual:
        raise RuntimeError("E1_DATABASE_URL_MISSING")
    # Do not use the diagnostic scratch database that existing Railway
    # phase-C jobs access via CC_PROBE_DATABASE_URL.
    if env.get("CC_PROBE_DATABASE_URL") and actual == env["CC_PROBE_DATABASE_URL"]:
        raise RuntimeError("E1_DATABASE_URL_IS_PROBE")
    expected = (env.get("STAGING_EXPECTED_DATABASE_HOST") or "").strip().lower()
    parsed = urlsplit(read_only_url(actual))
    host = (parsed.hostname or "").lower()
    if not expected or host != expected or not host.endswith(".railway.internal"):
        # Railway OAuth redacts variable values. Emit only validated internal
        # DNS labels (never user, password, full URL, database or query string)
        # to distinguish a stale expected host from the wrong database.
        def safe_host(value: str) -> str:
            if (
                len(value) <= 128 and value.endswith(".railway.internal")
                and re.fullmatch(r"[a-z0-9.-]+", value)
            ):
                return value
            return "NOT_PRIVATE_RAILWAY_HOST"
        print("E1_HOST_CONTRACT " + json.dumps({
            "database_host": safe_host(host),
            "expected_host": safe_host(expected),
            "match": False,
        }, sort_keys=True), flush=True)
        raise RuntimeError("E1_DATABASE_HOST_MISMATCH")
    if (parsed.port or 5432) != 5432 or not parsed.path.strip("/"):
        raise RuntimeError("E1_DATABASE_ENDPOINT_INVALID")
    return actual


def _grouped(report: dict, name: str) -> list[dict]:
    return [
        {
            "key": item["key"], "trades": item["trades"],
            "net_pnl": item["net_pnl"],
            "profit_factor": item["profit_factor"],
            "enough_for_ranking": item["enough_for_ranking"],
        }
        for item in report[name]
    ]


async def audit_once() -> dict:
    database_url = verify_environment(dict(os.environ))
    return await export_forensics(
        database_url, lookback_days=30, order_limit=250, agent_limit=20000,
    )


def main() -> int:
    try:
        data = asyncio.run(audit_once())
        report = data["report"]
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        compressed = gzip.compress(payload, compresslevel=9, mtime=0)
        summary = {
            "source": "POSTGRESQL_READ_ONLY", "mode": report["mode"],
            "closed_trades": report["closed_trades"],
            "open_trades": report["open_trades"],
            "net_pnl": report["net_pnl"], "win_rate": report["win_rate"],
            "profit_factor": report["profit_factor"],
            "missing_audit_closures": report["missing_audit_closures"],
            "missing_decision_traces": report["missing_decision_traces"],
            "missing_risk_traces": report["missing_risk_traces"],
            "flags_count": report["flags_count"],
            "by_strategy": _grouped(report, "by_strategy"),
            "by_symbol": _grouped(report, "by_symbol"),
            "by_timeframe": _grouped(report, "by_timeframe"),
            "by_exit_reason": _grouped(report, "by_exit_reason"),
            "order_sample_truncated": data["order_sample_truncated"],
            "agents_sample_truncated": data["agents_sample_truncated"],
            "archive_reconciliation": data["archive_reconciliation"],
            "agent_history_coverage": data["agent_history_coverage"],
            "agent_attribution_verified": False, "execution_authorized": False,
        }
        print("E1_AUDIT_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
        # Railway diagnostic service has no persistent volume. Stream a
        # checksummed compressed report to its private logs, not public GitHub.
        # The operator must reassemble all chunks and verify SHA-256.
        if len(compressed) > 120000:
            print("E1_EVIDENCE_TOO_LARGE_FOR_LOG_STREAM", flush=True)
            return 3
        encoded = base64.b64encode(compressed).decode("ascii")
        pieces = [encoded[i:i+2500] for i in range(0, len(encoded), 2500)]
        print("E1_AUDIT_MANIFEST " + json.dumps({
            "sha256": digest, "bytes": len(payload),
            "gzip_bytes": len(compressed), "chunks": len(pieces),
            "encoding": "gzip+base64", "record_type": "PAPER_E1_FORENSICS",
        }, sort_keys=True), flush=True)
        for index, chunk in enumerate(pieces, 1):
            print(f"E1_AUDIT_CHUNK {index}/{len(pieces)} {chunk}", flush=True)
        print("E1_AUDIT_COMPLETE", flush=True)
        return 0
    except Exception as exc:
        # Never log arbitrary exception text: SQL drivers may echo DB URLs.
        # Only explicitly enumerated static guard codes are safe to reveal.
        guard_codes = {
            "E1_NOT_STAGING", "E1_WRONG_RAILWAY_ENVIRONMENT",
            "E1_NOT_PAPER", "E1_OMS_ENVIRONMENT_NOT_PAPER",
            "E1_TESTNET_ENABLED", "E1_DATABASE_URL_MISSING",
            "E1_DATABASE_URL_IS_PROBE", "E1_DATABASE_HOST_MISMATCH",
            "E1_DATABASE_ENDPOINT_INVALID",
            "E1_TRANSACTION_NOT_READ_ONLY",
        }
        code = str(exc) if isinstance(exc, RuntimeError) and str(exc) in guard_codes else None
        print("E1_AUDIT_BLOCKED " + (code or type(exc).__name__), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""E2.6.1 bounded read-only diagnostics of already-persisted PAPER candles.

Uses the same <=300 rows obtained by the E2.6 READ ONLY auditor. This
does not ingest/backfill candles, modify the quality gate, authenticate
an exchange, or create prospective evidence.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re

from app.strategy.candidate_replay import BAR_SECONDS
from app.strategy.prospective_core_gate_e26 import CAMPAIGN, MARKETS

MAX_ROWS = CAMPAIGN["warmup_candles_per_market"]
MAX_EXAMPLES = 5
RECENT_HOURS = 24
_VALID_FOR_EXISTING_GATE = frozenset({"VALID", "ACCEPTED"})
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _safe_code(value: object) -> str:
    """Never expose arbitrary raw warning text or identifiers in auditor logs."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return "UNRECOGNIZED_CODE"


def _codes(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return ["MALFORMED_CODE_LIST"]
    result = [_safe_code(v) for v in value[:32]]
    if len(value) > 32:
        result.append("CODE_LIST_TRUNCATED")
    return result


def diagnose_candle_rows(
    rows, *, symbol: str, timeframe: str, now: datetime,
) -> dict:
    """Read-only diagnostics; no external source attestation or gate changes.

    Late received_at / recorded_at cannot distinguish backfills from delayed
    live collection. Recent 24h is reported separately to avoid treating older
    warmup bars as evidence of current feed health. This remains observational.
    """
    if (symbol, timeframe) not in MARKETS:
        raise ValueError("Market not in frozen E2.6 campaign")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Timezone-aware clock required")
    if len(rows) > MAX_ROWS:
        raise ValueError("Unbounded E2.6 diagnostic input")
    now = now.astimezone(timezone.utc)
    step = timedelta(seconds=BAR_SECONDS[timeframe])
    recent_cutoff = now - timedelta(hours=RECENT_HOURS)
    statuses, warnings, errors = Counter(), Counter(), Counter()
    recent_statuses = Counter()
    late_receive = late_record = late_persist_only = 0
    recent_n = recent_late_receive = recent_late_record = 0
    recent_unaccepted = 0
    invalid_clock = 0
    quality_examples = []
    late_examples = []
    sorted_rows = sorted(rows, key=lambda r: r.closed_at, reverse=True)
    seen = set()

    for ob in sorted_rows:
        closed, received, recorded = ob.closed_at, ob.received_at, ob.recorded_at
        if any(t.tzinfo is None or t.utcoffset() is None
               for t in (closed, received, recorded)):
            raise ValueError("Naive stored candle timestamp")
        closed, received, recorded = (
            closed.astimezone(timezone.utc),
            received.astimezone(timezone.utc),
            recorded.astimezone(timezone.utc),
        )
        if (closed in seen or ob.exchange != "BINANCE"
                or ob.symbol != symbol or ob.timeframe != timeframe):
            raise ValueError("Duplicate/mismatched stored candle")
        seen.add(closed)
        status = _safe_code(ob.quality_status)
        row_warnings = _codes(getattr(ob, "quality_warnings", None))
        row_errors = _codes(getattr(ob, "quality_errors", None))
        statuses[status] += 1
        warnings.update(set(row_warnings))  # per-candle incidence, not repeats
        errors.update(set(row_errors))
        receive_ok = closed <= received <= closed + step
        record_ok = closed <= recorded <= closed + step
        if received > closed + step:
            late_receive += 1
        if recorded > closed + step:
            late_record += 1
            if receive_ok:
                late_persist_only += 1
        if received < closed or recorded < closed or received > now or recorded > now:
            invalid_clock += 1
        if closed >= recent_cutoff:
            recent_n += 1
            recent_statuses[status] += 1
            recent_late_receive += int(not receive_ok)
            recent_late_record += int(not record_ok)
            recent_unaccepted += int(status not in _VALID_FOR_EXISTING_GATE)
        if status not in _VALID_FOR_EXISTING_GATE and len(quality_examples) < MAX_EXAMPLES:
            quality_examples.append({
                "closed_at_utc": closed.isoformat(),
                "status": status,
                "score": getattr(ob, "quality_score", None),
                "warning_codes": sorted(set(row_warnings)),
                "error_codes": sorted(set(row_errors)),
            })
        if (not receive_ok or not record_ok) and len(late_examples) < MAX_EXAMPLES:
            late_examples.append({
                "closed_at_utc": closed.isoformat(),
                "received_lag_seconds": round((received - closed).total_seconds(), 3),
                "recorded_lag_seconds": round((recorded - closed).total_seconds(), 3),
            })

    return {
        "mode": "E261_PERSISTED_PAPER_QUALITY_DIAGNOSTICS_READ_ONLY",
        "symbol": symbol,
        "timeframe": timeframe,
        "sampled_rows": len(rows),
        "warmup_shortfall_rows": max(0, MAX_ROWS - len(rows)),
        "quality_status_counts": dict(sorted(statuses.items())),
        "warning_code_candle_counts": dict(sorted(warnings.items())),
        "error_code_candle_counts": dict(sorted(errors.items())),
        "quality_examples_newest_first": quality_examples,
        "late_examples_newest_first": late_examples,
        "late_receipt_rows": late_receive,
        "late_persistence_rows": late_record,
        "late_persistence_with_timely_receipt_rows": late_persist_only,
        "invalid_clock_rows": invalid_clock,
        "recent_window_hours": RECENT_HOURS,
        "recent_window_rows": recent_n,
        "recent_window_quality_status_counts": dict(sorted(recent_statuses.items())),
        "recent_window_unaccepted_quality_rows": recent_unaccepted,
        "recent_window_untimely_receipt_rows": recent_late_receive,
        "recent_window_untimely_persistence_rows": recent_late_record,
        "historical_backfill_vs_live_delay": "CANNOT_DETERMINE_FROM_DATABASE_TIMESTAMPS",
        "source_externally_authenticated": False,
        "collector_prospectively_attested": False,
        "can_promote_strategy": False,
        "execution_authorized": False,
        "readiness_gate_modified": False,
    }

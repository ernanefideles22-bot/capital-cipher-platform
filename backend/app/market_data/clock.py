"""NTP-style clock offset evaluation for external market-data sources."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.schemas.data_catalog import ClockObservation


def evaluate_clock_probe(
    *,
    source: str,
    request_started_at: datetime,
    source_at: datetime,
    response_received_at: datetime,
    warning_offset_ms: float = 500.0,
    unsafe_offset_ms: float = 2_000.0,
    warning_round_trip_ms: float = 1_000.0,
    unsafe_round_trip_ms: float = 5_000.0,
) -> ClockObservation:
    """Estimate clock offset at the local request/response midpoint."""
    for name, value in (
        ("request_started_at", request_started_at),
        ("source_at", source_at),
        ("response_received_at", response_received_at),
    ):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    if response_received_at < request_started_at:
        raise ValueError("response_received_at must not precede request_started_at")
    if not 0 <= warning_offset_ms <= unsafe_offset_ms:
        raise ValueError("clock offset thresholds are inconsistent")
    if not 0 <= warning_round_trip_ms <= unsafe_round_trip_ms:
        raise ValueError("round-trip thresholds are inconsistent")

    round_trip_ms = (
        response_received_at - request_started_at
    ).total_seconds() * 1_000
    midpoint = request_started_at + (
        response_received_at - request_started_at
    ) / 2
    offset_ms = (source_at - midpoint).total_seconds() * 1_000

    if (
        abs(offset_ms) > unsafe_offset_ms
        or round_trip_ms > unsafe_round_trip_ms
    ):
        status = "UNSAFE"
    elif (
        abs(offset_ms) > warning_offset_ms
        or round_trip_ms > warning_round_trip_ms
    ):
        status = "WARNING"
    else:
        status = "SYNCED"

    identity = "|".join(
        (
            source,
            request_started_at.astimezone(timezone.utc).isoformat(),
            source_at.astimezone(timezone.utc).isoformat(),
            response_received_at.astimezone(timezone.utc).isoformat(),
        )
    ).encode("utf-8")
    return ClockObservation(
        observation_id=hashlib.sha256(identity).hexdigest(),
        source=source,
        request_started_at=request_started_at,
        source_at=source_at,
        response_received_at=response_received_at,
        offset_ms=offset_ms,
        round_trip_ms=round_trip_ms,
        status=status,
    )

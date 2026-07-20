"""NTP-style clock offset evaluation for external market-data sources."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from app.core.errors import DataQualityError
from app.core.logging import ServiceLogger
from app.schemas.data_catalog import ClockObservation
from app.schemas.common import Exchange

logger = ServiceLogger("exchange_clock")


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


@dataclass(frozen=True)
class ClockTrustVerdict:
    exchange: Exchange
    status: str
    trusted: bool
    observation: ClockObservation | None
    reason: str


class ExchangeClockRegistry:
    """In-memory view of the latest persisted clock evidence per exchange."""

    def __init__(self, *, max_age_seconds: float = 90.0) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._max_age_seconds = max_age_seconds
        self._latest: dict[Exchange, ClockObservation] = {}

    def record(
        self,
        exchange: Exchange | str,
        observation: ClockObservation,
    ) -> None:
        self._latest[Exchange(exchange)] = observation

    def verdict(
        self,
        exchange: Exchange | str,
        *,
        now: datetime | None = None,
    ) -> ClockTrustVerdict:
        exchange = Exchange(exchange)
        observation = self._latest.get(exchange)
        if observation is None:
            return ClockTrustVerdict(
                exchange=exchange,
                status="UNKNOWN",
                trusted=False,
                observation=None,
                reason="No clock observation is available",
            )
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        age_seconds = (
            now.astimezone(timezone.utc)
            - observation.response_received_at.astimezone(timezone.utc)
        ).total_seconds()
        if age_seconds < 0 or age_seconds > self._max_age_seconds:
            return ClockTrustVerdict(
                exchange=exchange,
                status="UNKNOWN",
                trusted=False,
                observation=observation,
                reason="Clock observation is stale or from the future",
            )
        trusted = observation.status in {"SYNCED", "WARNING"}
        return ClockTrustVerdict(
            exchange=exchange,
            status=observation.status,
            trusted=trusted,
            observation=observation,
            reason=(
                "Clock evidence is within the ingestion gate"
                if trusted
                else "Clock offset or round-trip latency is unsafe"
            ),
        )

    def require_trusted(
        self,
        exchange: Exchange | str,
        *,
        now: datetime | None = None,
    ) -> ClockTrustVerdict:
        verdict = self.verdict(exchange, now=now)
        if not verdict.trusted:
            raise DataQualityError(
                "Trusted market-data ingestion blocked by clock gate",
                metadata={
                    "exchange": verdict.exchange.value,
                    "clock_status": verdict.status,
                    "reason": verdict.reason,
                },
            )
        return verdict


class ClockProbeClient(Protocol):
    exchange: Exchange
    source_name: str

    async def probe_clock(
        self,
        *,
        warning_offset_ms: float,
        unsafe_offset_ms: float,
        warning_round_trip_ms: float,
        unsafe_round_trip_ms: float,
    ) -> ClockObservation: ...


class ClockObservationRepository(Protocol):
    async def save_clock_observation(
        self,
        observation: ClockObservation,
    ) -> bool: ...


class ExchangeClockMonitor:
    """Probe public exchange clocks and publish only persisted observations."""

    def __init__(
        self,
        clients: dict[Exchange, ClockProbeClient],
        registry: ExchangeClockRegistry,
        repository: ClockObservationRepository,
        *,
        interval_seconds: float = 30.0,
        warning_offset_ms: float = 500.0,
        unsafe_offset_ms: float = 2_000.0,
        warning_round_trip_ms: float = 1_000.0,
        unsafe_round_trip_ms: float = 5_000.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._clients = clients
        self._registry = registry
        self._repository = repository
        self._interval_seconds = interval_seconds
        self._thresholds = {
            "warning_offset_ms": warning_offset_ms,
            "unsafe_offset_ms": unsafe_offset_ms,
            "warning_round_trip_ms": warning_round_trip_ms,
            "unsafe_round_trip_ms": unsafe_round_trip_ms,
        }

    async def probe(self, exchange: Exchange | str) -> ClockObservation:
        exchange = Exchange(exchange)
        observation = await self._clients[exchange].probe_clock(**self._thresholds)
        await self._repository.save_clock_observation(observation)
        self._registry.record(exchange, observation)
        return observation

    async def run(self, stop_event) -> None:
        """Continuously refresh all sources; failed probes age out naturally."""
        import asyncio

        while not stop_event.is_set():
            exchanges = list(self._clients)
            results = await asyncio.gather(
                *(self.probe(exchange) for exchange in exchanges),
                return_exceptions=True,
            )
            for exchange, result in zip(exchanges, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "Exchange clock probe failed",
                        event_type="CLOCK_PROBE_FAILED",
                        metadata={
                            "exchange": exchange.value,
                            "error_type": type(result).__name__,
                        },
                    )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue

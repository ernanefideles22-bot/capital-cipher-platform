"""Governed public Binance USD-M evidence for derivatives SHADOW specialists.

The collector is deliberately read-only and historical-as-of: every provider
request is capped at the agent candle timestamp so external evidence cannot
look into the future when the PAPER pipeline is processing a backlog.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.errors import ExternalServiceError
from app.core.logging import ServiceLogger
from app.schemas.specialist_evaluation import SpecialistEvidence

logger = ServiceLogger("binance_derivatives_evidence")

BINANCE_USDM_REST_URL = "https://fapi.binance.com"
_SUPPORTED_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}


def _payload_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_from_ms(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000, tz=timezone.utc)


class BinanceUsdMDerivativesEvidenceCollector:
    """Fetch a small, governed public derivatives snapshot once per candle.

    Five metrics are normalized into the existing SpecialistEvidence contract:
    funding_rate, open_interest_change, basis, long_short_ratio and
    taker_buy_sell_ratio. The collector has no decision or order authority.
    """

    source_name = "binance.usdm.public-rest"

    def __init__(
        self,
        evidence_service,
        *,
        base_url: str = BINANCE_USDM_REST_URL,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._evidence_service = evidence_service
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = client
        self._locks: dict[str, asyncio.Lock] = {}
        self._attempted: set[tuple[str, str, int]] = set()

    @staticmethod
    def _period(timeframe: str) -> str:
        return timeframe if timeframe in _SUPPORTED_PERIODS else "5m"

    async def refresh(
        self,
        *,
        scope: str,
        as_of: datetime,
        timeframe: str,
    ) -> None:
        """Populate available evidence for one symbol without raising upstream."""

        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        symbol = scope.upper()
        if not symbol.endswith("USDT"):
            return
        period = self._period(timeframe)
        as_of_utc = as_of.astimezone(timezone.utc)
        bucket = int(as_of_utc.timestamp()) // max(60, self._period_seconds(period))
        attempt_key = (symbol, period, bucket)
        if attempt_key in self._attempted:
            return
        lock = self._locks.setdefault(symbol, asyncio.Lock())
        async with lock:
            if attempt_key in self._attempted:
                return
            try:
                await self._refresh_locked(
                    symbol=symbol,
                    as_of=as_of_utc,
                    period=period,
                )
            finally:
                # A failed provider call must not make all 15 derivatives agents
                # hammer the same public endpoint in the same candle cycle.
                self._attempted.add(attempt_key)

    @staticmethod
    def _period_seconds(period: str) -> int:
        amount = int(period[:-1])
        return amount * {"m": 60, "h": 3_600, "d": 86_400}[period[-1]]

    async def _refresh_locked(
        self,
        *,
        symbol: str,
        as_of: datetime,
        period: str,
    ) -> None:
        end_time = int(as_of.timestamp() * 1_000)
        requests = (
            (
                "funding_rate",
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "endTime": end_time, "limit": 1},
            ),
            (
                "open_interest_change",
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": period, "endTime": end_time, "limit": 2},
            ),
            (
                "basis",
                "/futures/data/basis",
                {
                    "pair": symbol,
                    "contractType": "PERPETUAL",
                    "period": period,
                    "endTime": end_time,
                    "limit": 1,
                },
            ),
            (
                "long_short_ratio",
                "/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": period, "endTime": end_time, "limit": 1},
            ),
            (
                "taker_buy_sell_ratio",
                "/futures/data/takerlongshortRatio",
                {"symbol": symbol, "period": period, "endTime": end_time, "limit": 1},
            ),
        )

        async def fetch_all(client: httpx.AsyncClient):
            return await asyncio.gather(
                *(
                    self._get_json(client, path, params=params)
                    for _, path, params in requests
                ),
                return_exceptions=True,
            )

        if self._client is None:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                headers={"User-Agent": "capital-cipher-platform/0.26"},
            ) as client:
                results = await fetch_all(client)
        else:
            results = await fetch_all(self._client)

        for (metric_name, path, _), payload in zip(requests, results, strict=True):
            if isinstance(payload, Exception):
                logger.warning(
                    "Public derivatives evidence request failed",
                    event_type="DERIVATIVES_EVIDENCE_FETCH_FAILED",
                    metadata={
                        "symbol": symbol,
                        "metric_name": metric_name,
                        "error_type": type(payload).__name__,
                    },
                )
                continue
            try:
                parsed = self._parse_metric(metric_name, payload)
            except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
                logger.warning(
                    "Public derivatives evidence payload was invalid",
                    event_type="DERIVATIVES_EVIDENCE_INVALID",
                    metadata={
                        "symbol": symbol,
                        "metric_name": metric_name,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            if parsed is None:
                continue
            value, observed_at, event_payload, quality_score = parsed
            if observed_at > as_of:
                # Provider data newer than the evaluated candle is never admitted.
                logger.warning(
                    "Future derivatives evidence rejected",
                    event_type="DERIVATIVES_EVIDENCE_LOOKAHEAD_REJECTED",
                    metadata={"symbol": symbol, "metric_name": metric_name},
                )
                continue
            source_event_id = (
                f"{path}:{symbol}:{metric_name}:{int(observed_at.timestamp() * 1000)}"
            )
            existing = await self._evidence_service.latest(
                domain="DERIVATIVES",
                metric_name=metric_name,
                scope=symbol,
                as_of=observed_at,
            )
            if existing is not None and existing.source_event_id == source_event_id:
                continue
            received_at = max(datetime.now(timezone.utc), observed_at)
            evidence = SpecialistEvidence(
                domain="DERIVATIVES",
                metric_name=metric_name,
                scope=symbol,
                source=self.source_name,
                source_event_id=source_event_id,
                value=value,
                unit="ratio",
                quality_score=quality_score,
                observed_at=observed_at,
                received_at=received_at,
                provenance_uri=f"{self._base_url}{path}",
                payload_sha256=_payload_sha256(event_payload),
            )
            await self._evidence_service.ingest(evidence)

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any],
    ) -> Any:
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status_code = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            raise ExternalServiceError(
                "Binance USD-M public derivatives request failed",
                metadata={
                    "provider": "BINANCE_USDM",
                    "status_code": status_code,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    @staticmethod
    def _parse_metric(
        metric_name: str,
        payload: Any,
    ) -> tuple[float, datetime, Any, int] | None:
        if not isinstance(payload, list) or not payload:
            return None
        rows = sorted(payload, key=lambda row: int(row["timestamp"] if "timestamp" in row else row["fundingTime"]))
        if metric_name == "funding_rate":
            row = rows[-1]
            return float(row["fundingRate"]), _utc_from_ms(row["fundingTime"]), row, 100
        if metric_name == "open_interest_change":
            if len(rows) < 2:
                return None
            previous, current = rows[-2], rows[-1]
            previous_value = float(previous["sumOpenInterest"])
            current_value = float(current["sumOpenInterest"])
            if previous_value == 0:
                return None
            value = current_value / previous_value - 1
            return value, _utc_from_ms(current["timestamp"]), rows[-2:], 95
        row = rows[-1]
        if metric_name == "basis":
            value = float(row["basisRate"])
        elif metric_name == "long_short_ratio":
            value = float(row["longShortRatio"])
        elif metric_name == "taker_buy_sell_ratio":
            value = float(row["buySellRatio"])
        else:
            return None
        return value, _utc_from_ms(row["timestamp"]), row, 100

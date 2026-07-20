"""Binance public REST client for server time and historical spot candles."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from app.core.errors import ExternalServiceError
from app.market_data.adapters.public_rest import RawPageHandler
from app.market_data.clock import evaluate_clock_probe
from app.market_data.data_quality import TIMEFRAME_SECONDS
from app.schemas.common import Exchange
from app.schemas.data_catalog import ClockObservation
from app.schemas.data_lake import RawProviderPage
from app.schemas.market import Candle

BINANCE_PUBLIC_REST_URL = "https://data-api.binance.vision"


class BinancePublicRestClient:
    exchange = Exchange.BINANCE
    source_name = "binance.public-rest"

    def __init__(
        self,
        *,
        base_url: str = BINANCE_PUBLIC_REST_URL,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "capital-cipher-platform/0.13"},
        )

    async def _get_json(self, path: str, *, params: dict | None = None):
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status_code = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            raise ExternalServiceError(
                "Binance public market-data request failed",
                metadata={
                    "provider": "BINANCE",
                    "status_code": status_code,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    async def probe_clock(
        self,
        *,
        warning_offset_ms: float = 500.0,
        unsafe_offset_ms: float = 2_000.0,
        warning_round_trip_ms: float = 1_000.0,
        unsafe_round_trip_ms: float = 5_000.0,
    ) -> ClockObservation:
        started = datetime.now(timezone.utc)
        payload = await self._get_json("/api/v3/time")
        received = datetime.now(timezone.utc)
        try:
            source_at = datetime.fromtimestamp(
                int(payload["serverTime"]) / 1_000,
                tz=timezone.utc,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExternalServiceError(
                "Binance server-time payload is invalid",
                metadata={"provider": "BINANCE"},
            ) from exc
        return evaluate_clock_probe(
            source="binance.server-time",
            request_started_at=started,
            source_at=source_at,
            response_received_at=received,
            warning_offset_ms=warning_offset_ms,
            unsafe_offset_ms=unsafe_offset_ms,
            warning_round_trip_ms=warning_round_trip_ms,
            unsafe_round_trip_ms=unsafe_round_trip_ms,
        )

    async def fetch_candles(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        limit: int,
        on_page: RawPageHandler | None = None,
    ) -> list[Candle]:
        step_seconds = TIMEFRAME_SECONDS.get(timeframe)
        if step_seconds is None:
            raise ValueError(f"Unsupported Binance timeframe: {timeframe}")
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("Historical range must be timezone-aware")
        if start_at > end_at:
            raise ValueError("start_at must not be after end_at")
        if limit < 1 or limit > 1_000_000:
            raise ValueError("limit must be between 1 and 1000000")

        step_ms = step_seconds * 1_000
        start_close_ms = int(start_at.timestamp() * 1_000)
        end_close_ms = int(end_at.timestamp() * 1_000)
        cursor = start_close_ms - step_ms + 1
        end_open_ms = end_close_ms - step_ms + 1
        received_at = datetime.now(timezone.utc)
        candles: dict[int, Candle] = {}
        page_index = 0

        while cursor <= end_open_ms and len(candles) < limit:
            page_limit = min(1_000, limit - len(candles))
            request_params = {
                "symbol": symbol.upper(),
                "interval": timeframe,
                "startTime": cursor,
                "endTime": end_open_ms,
                "limit": page_limit,
            }
            payload = await self._get_json(
                "/api/v3/klines",
                params=request_params,
            )
            if not isinstance(payload, list):
                raise ExternalServiceError(
                    "Binance kline payload is invalid",
                    metadata={"provider": "BINANCE"},
                )
            if on_page is not None:
                await on_page(
                    RawProviderPage(
                        source=self.source_name,
                        endpoint="/api/v3/klines",
                        request_params=request_params,
                        payload=payload,
                        page_index=page_index,
                    )
                )
            page_index += 1
            if not payload:
                break
            try:
                for row in payload:
                    close_ms = int(row[6])
                    if start_close_ms <= close_ms <= end_close_ms:
                        candles[close_ms] = Candle(
                            exchange=Exchange.BINANCE,
                            symbol=symbol.upper(),
                            timeframe=timeframe,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            closed_at=datetime.fromtimestamp(
                                close_ms / 1_000,
                                tz=timezone.utc,
                            ),
                            received_at=received_at,
                        )
                next_cursor = int(payload[-1][0]) + step_ms
            except (IndexError, TypeError, ValueError) as exc:
                raise ExternalServiceError(
                    "Binance kline payload is invalid",
                    metadata={"provider": "BINANCE"},
                ) from exc
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        return [candles[key] for key in sorted(candles)]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

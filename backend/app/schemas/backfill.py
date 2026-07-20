"""Versioned contracts for market-data continuity and historical backfills."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.schemas.common import Exchange, utcnow
from app.schemas.events import CONTRACT_VERSION

GapStatus = Literal["OPEN", "FILLING", "RESOLVED", "FAILED"]
BackfillStatus = Literal[
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "PARTIAL",
    "BLOCKED",
    "FAILED",
]


class MarketDataGap(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    gap_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    missing_count: int = Field(gt=0)
    status: GapStatus = "OPEN"
    detected_at: AwareDatetime = Field(default_factory=utcnow)
    resolved_at: AwareDatetime | None = None
    backfill_job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_gap(self) -> "MarketDataGap":
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        if self.status == "RESOLVED" and self.resolved_at is None:
            raise ValueError("resolved gaps require resolved_at")
        return self


class GapScanRequest(BaseModel):
    exchange: Exchange = Exchange.BINANCE
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(default="15m", pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    limit: int = Field(default=100_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_range(self) -> "GapScanRequest":
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        return self


class HistoricalBackfillRequest(BaseModel):
    """Inclusive range expressed in normalized candle close timestamps."""

    exchange: Exchange = Exchange.BINANCE
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(default="15m", pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    max_candles: int = Field(default=100_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_range(self) -> "HistoricalBackfillRequest":
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        return self


class HistoricalBackfillJob(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    status: BackfillStatus = "PENDING"
    retrieved_count: int = Field(default=0, ge=0)
    inserted_count: int = Field(default=0, ge=0)
    remaining_gap_count: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    dataset_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    clock_observation_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    clock_status: Literal["SYNCED", "WARNING", "UNSAFE", "UNKNOWN"] = "UNKNOWN"
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]+$")
    error_message: str | None = Field(default=None, max_length=500)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    updated_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_job(self) -> "HistoricalBackfillJob":
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        if self.job_id != self.request_fingerprint:
            raise ValueError("job_id must equal request_fingerprint")
        if self.status in {"COMPLETED", "PARTIAL", "BLOCKED", "FAILED"}:
            if self.completed_at is None:
                raise ValueError("terminal backfill jobs require completed_at")
        return self


def backfill_request_fingerprint(request: HistoricalBackfillRequest) -> str:
    """Derive an idempotency key from the normalized historical selection."""
    payload = {
        "contract_version": CONTRACT_VERSION,
        "exchange": request.exchange.value,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "start_at": request.start_at.isoformat(),
        "end_at": request.end_at.isoformat(),
        "max_candles": request.max_candles,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

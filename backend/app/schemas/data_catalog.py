"""Versioned contracts for clock quality and deterministic data catalogs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.schemas.common import Exchange, utcnow
from app.schemas.events import CONTRACT_VERSION

ClockStatus = Literal["SYNCED", "WARNING", "UNSAFE"]


class ClockObservation(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    observation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    request_started_at: AwareDatetime
    source_at: AwareDatetime
    response_received_at: AwareDatetime
    offset_ms: float
    round_trip_ms: float = Field(ge=0)
    status: ClockStatus
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_probe_order(self) -> "ClockObservation":
        if self.response_received_at < self.request_started_at:
            raise ValueError("response_received_at must not precede request_started_at")
        return self


class CandleDatasetManifest(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    dataset_id: str = Field(pattern=r"^candles:v1:[a-f0-9]{64}$")
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candle_contract_version: Literal["1.0.0"] = CONTRACT_VERSION
    dataset_type: Literal["CANDLES"] = "CANDLES"
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    row_count: int = Field(gt=0)
    selection: dict[str, Any]
    quality_summary: dict[str, Any]
    clock_status: Literal["SYNCED", "WARNING", "UNSAFE", "UNKNOWN"] = "UNKNOWN"
    created_at: AwareDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_manifest(self) -> "CandleDatasetManifest":
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        if self.dataset_id != f"candles:v1:{self.dataset_hash}":
            raise ValueError("dataset_id must be derived from dataset_hash")
        return self


class CandleDatasetRequest(BaseModel):
    exchange: Exchange = Exchange.BINANCE
    symbol: str = Field(default="BTCUSDT", pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(default="15m", pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None
    limit: int = Field(default=100_000, ge=1, le=1_000_000)
    clock_status: Literal["SYNCED", "WARNING", "UNSAFE", "UNKNOWN"] = "UNKNOWN"

    @model_validator(mode="after")
    def validate_range(self) -> "CandleDatasetRequest":
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.start_at > self.end_at
        ):
            raise ValueError("start_at must not be after end_at")
        return self

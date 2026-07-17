"""Market data schemas (docs/29-domain-model.md, contracts/market-candle.schema.json)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Exchange, utcnow
from app.schemas.events import CONTRACT_VERSION


class Candle(BaseModel):
    """OHLCV candle with domain invariants enforced (docs/29, docs/32)."""

    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    closed_at: datetime
    received_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_invariants(self) -> "Candle":
        if not (self.high >= self.open and self.high >= self.close and self.high >= self.low):
            raise ValueError("Invalid candle: high must be >= open, close and low")
        if not (self.low <= self.open and self.low <= self.close and self.low <= self.high):
            raise ValueError("Invalid candle: low must be <= open, close and high")
        return self


class RawMarketEvent(BaseModel):
    """Lossless public market payload captured before normalization."""

    event_id: str = ""
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    source: str = Field(min_length=1)
    exchange: Exchange
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    symbol: str | None = Field(default=None, pattern=r"^[A-Z0-9._-]{2,32}$")
    occurred_at: datetime | None = None
    received_at: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any]
    payload_sha256: str = ""

    @model_validator(mode="after")
    def assign_identity_and_verify_checksum(self) -> "RawMarketEvent":
        encoded = json.dumps(
            self.payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        if self.payload_sha256 and self.payload_sha256 != checksum:
            raise ValueError("payload_sha256 does not match the raw payload")
        self.payload_sha256 = checksum
        if not self.event_id:
            self.event_id = str(uuid5(NAMESPACE_URL, f"{self.source}:{checksum}"))
        return self


class MarketEvent(BaseModel):
    event_type: str
    exchange: Exchange
    symbol: str
    timeframe: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    market_data: dict = Field(default_factory=dict)


class DataQualityReport(BaseModel):
    """Output of the data quality module (docs/32-data-quality.md)."""

    data_quality_score: int = Field(ge=0, le=100)
    status: str
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

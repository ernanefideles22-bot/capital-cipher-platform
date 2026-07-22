"""Contracts for durable backfill queues and content-addressed raw data."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.schemas.backfill import HistoricalBackfillRequest
from app.schemas.common import Exchange, utcnow
from app.schemas.events import CONTRACT_VERSION

QueueStatus = Literal[
    "PENDING",
    "LEASED",
    "RETRY",
    "COMPLETED",
    "DEAD_LETTER",
]


class BackfillQueueItem(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    queue_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    exchange: Exchange
    symbol: str = Field(pattern=r"^[A-Z0-9._-]{2,32}$")
    timeframe: str = Field(pattern=r"^[1-9][0-9]*[mhdw]$")
    start_at: AwareDatetime
    end_at: AwareDatetime
    max_candles: int = Field(ge=1, le=1_000_000)
    status: QueueStatus = "PENDING"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1, le=100)
    available_at: AwareDatetime = Field(default_factory=utcnow)
    leased_by: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$",
    )
    lease_expires_at: AwareDatetime | None = None
    last_error_code: str | None = Field(
        default=None,
        pattern=r"^[A-Z][A-Z0-9_]+$",
    )
    last_error_message: str | None = Field(default=None, max_length=500)
    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_queue_item(self) -> "BackfillQueueItem":
        if self.queue_id != self.job_id:
            raise ValueError("queue_id must equal job_id")
        if self.start_at > self.end_at:
            raise ValueError("start_at must not be after end_at")
        if self.attempt_count > self.max_attempts:
            raise ValueError("attempt_count must not exceed max_attempts")
        if self.status == "LEASED":
            if self.leased_by is None or self.lease_expires_at is None:
                raise ValueError("leased items require owner and expiry")
        elif self.leased_by is not None or self.lease_expires_at is not None:
            raise ValueError("only leased items may have lease metadata")
        if self.status in {"COMPLETED", "DEAD_LETTER"}:
            if self.completed_at is None:
                raise ValueError("terminal queue items require completed_at")
        elif self.completed_at is not None:
            raise ValueError("non-terminal queue items cannot be completed")
        return self

    def to_request(self) -> HistoricalBackfillRequest:
        return HistoricalBackfillRequest(
            exchange=self.exchange,
            symbol=self.symbol,
            timeframe=self.timeframe,
            start_at=self.start_at,
            end_at=self.end_at,
            max_candles=self.max_candles,
        )


class RawProviderPage(BaseModel):
    """Lossless public REST response before candle normalization."""

    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    endpoint: str = Field(pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,255}$")
    request_params: dict[str, Any]
    payload: Any
    page_index: int = Field(ge=0)
    fetched_at: AwareDatetime = Field(default_factory=utcnow)


class RawDataObject(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    object_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    object_uri: str = Field(pattern=r"^lake://raw/[a-z0-9._/-]+\.json\.gz$")
    content_type: Literal["application/json"] = "application/json"
    content_encoding: Literal["gzip"] = "gzip"
    uncompressed_bytes: int = Field(gt=0)
    stored_bytes: int = Field(gt=0)
    created_at: AwareDatetime = Field(default_factory=utcnow)


class BackfillRawPageLink(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    page_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_count: int = Field(ge=1)
    page_index: int = Field(ge=0)
    object_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,127}$")
    endpoint: str = Field(pattern=r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,255}$")
    request_params: dict[str, Any]
    fetched_at: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utcnow)


def raw_page_id(
    *,
    job_id: str,
    attempt_count: int,
    page_index: int,
    object_hash: str,
) -> str:
    identity = (
        f"{job_id}|{attempt_count}|{page_index}|{object_hash}"
    ).encode("ascii")
    return hashlib.sha256(identity).hexdigest()

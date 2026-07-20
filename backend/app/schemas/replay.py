"""Deterministic replay checkpoint contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import utcnow
from app.schemas.events import CONTRACT_VERSION


class ReplayCheckpoint(BaseModel):
    schema_version: Literal["1.0.0"] = CONTRACT_VERSION
    replay_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{3,128}$")
    consumer_name: str = Field(pattern=r"^[A-Za-z0-9._:-]{3,128}$")
    topic: str = Field(pattern=r"^[a-z][a-z0-9_.-]+\.v[1-9][0-9]*$")
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    next_offset: int = Field(default=0, ge=0)
    last_event_id: str | None = None
    events_processed: int = Field(default=0, ge=0)
    status: Literal["RUNNING", "COMPLETED", "FAILED"] = "RUNNING"
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None


class ReplayResult(BaseModel):
    replay_id: str
    dataset_hash: str
    start_offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    events_emitted: int = Field(ge=0)
    total_events: int = Field(ge=0)
    resumed: bool
    completed: bool

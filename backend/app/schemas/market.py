"""Market data schemas (docs/29-domain-model.md, contracts/market-candle.schema.json)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import Exchange, utcnow


class Candle(BaseModel):
    """OHLCV candle with domain invariants enforced (docs/29, docs/32)."""

    exchange: Exchange
    symbol: str = Field(min_length=1)
    timeframe: str = Field(min_length=1)
    open: float
    high: float
    low: float
    close: float
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

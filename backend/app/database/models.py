"""SQLAlchemy models (docs/12-database-specification.md)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# JSONB on PostgreSQL, JSON elsewhere (SQLite in local dev).
JsonType = JSON().with_variant(JSONB(), "postgresql")
INTERNAL_SCHEMA = "capital_cipher"


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid4())


class SystemEventModel(Base):
    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    event_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class EventJournalModel(Base):
    __tablename__ = "event_journal"

    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class EventOutboxModel(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        Index(
            "ix_event_outbox_pending_created",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("event_journal.event_id", ondelete="CASCADE"),
        primary_key=True,
    )
    broker_message_id: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RawMarketEventModel(Base):
    __tablename__ = "raw_market_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(Text, index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class CandleObservationModel(Base):
    """Append-only, idempotent time-series candle storage."""

    __tablename__ = "candle_observations"
    __table_args__ = (
        UniqueConstraint(
            "exchange",
            "symbol",
            "timeframe",
            "closed_at",
            name="uq_candle_observations_series_time",
        ),
        CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_candle_observations_prices_positive",
        ),
        CheckConstraint(
            "high >= open AND high >= close AND high >= low",
            name="ck_candle_observations_high",
        ),
        CheckConstraint(
            "low <= open AND low <= close AND low <= high",
            name="ck_candle_observations_low",
        ),
        CheckConstraint(
            "volume >= 0",
            name="ck_candle_observations_volume",
        ),
        CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)",
            name="ck_candle_observations_quality_score",
        ),
        Index(
            "ix_candle_observations_series_time",
            "exchange",
            "symbol",
            "timeframe",
            "closed_at",
        ),
        Index(
            "ix_candle_observations_quality_received",
            "quality_status",
            "received_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    candle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    open: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    volume: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingest_lag_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    quality_score: Mapped[int | None] = mapped_column(Integer)
    quality_status: Mapped[str] = mapped_column(String(16), nullable=False)
    quality_warnings: Mapped[list] = mapped_column(JsonType, nullable=False)
    quality_errors: Mapped[list] = mapped_column(JsonType, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DatasetManifestModel(Base):
    """Immutable catalog entry for a deterministic candle selection."""

    __tablename__ = "dataset_manifests"
    __table_args__ = (
        CheckConstraint("row_count > 0", name="ck_dataset_manifests_row_count"),
        CheckConstraint("start_at <= end_at", name="ck_dataset_manifests_time_range"),
        Index(
            "ix_dataset_manifests_series_range",
            "exchange",
            "symbol",
            "timeframe",
            "start_at",
            "end_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    dataset_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    candle_contract_version: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selection: Mapped[dict] = mapped_column(JsonType, nullable=False)
    quality_summary: Mapped[dict] = mapped_column(JsonType, nullable=False)
    clock_status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WalkForwardExperimentModel(Base):
    """Append-only, content-addressed walk-forward research artifact."""

    __tablename__ = "walk_forward_experiments"
    __table_args__ = (
        CheckConstraint(
            "promotion_status = 'RESEARCH_ONLY'",
            name="ck_walk_forward_experiments_research_only",
        ),
        CheckConstraint(
            "artifact_version = 'walk-forward-artifact-v1'",
            name="ck_walk_forward_experiments_artifact_version",
        ),
        CheckConstraint(
            "length(dataset_hash) = 64",
            name="ck_walk_forward_experiments_dataset_hash",
        ),
        CheckConstraint(
            "length(artifact_hash) = 64",
            name="ck_walk_forward_experiments_artifact_hash",
        ),
        Index(
            "ix_walk_forward_experiments_candidate_created",
            "candidate_version",
            "created_at",
        ),
        Index(
            "ix_walk_forward_experiments_dataset_created",
            "dataset_hash",
            "created_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    row_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    experiment_id: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        unique=True,
    )
    artifact_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_version: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(96), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_version: Mapped[str] = mapped_column(Text, nullable=False)
    promotion_status: Mapped[str] = mapped_column(String(16), nullable=False)
    report_payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ClockObservationModel(Base):
    """NTP-style source clock comparison recorded for audit and gating."""

    __tablename__ = "clock_observations"
    __table_args__ = (
        CheckConstraint(
            "round_trip_ms >= 0",
            name="ck_clock_observations_round_trip",
        ),
        CheckConstraint(
            "status IN ('SYNCED', 'WARNING', 'UNSAFE')",
            name="ck_clock_observations_status",
        ),
        Index(
            "ix_clock_observations_source_time",
            "source",
            "response_received_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    request_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    response_received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    offset_ms: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    round_trip_ms: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketDataGapModel(Base):
    """Persisted continuity defect for one normalized candle series."""

    __tablename__ = "market_data_gaps"
    __table_args__ = (
        CheckConstraint(
            "missing_count > 0",
            name="ck_market_data_gaps_missing_count",
        ),
        CheckConstraint(
            "start_at <= end_at",
            name="ck_market_data_gaps_time_range",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'FILLING', 'RESOLVED', 'FAILED')",
            name="ck_market_data_gaps_status",
        ),
        Index(
            "ix_market_data_gaps_series_status_range",
            "exchange",
            "symbol",
            "timeframe",
            "status",
            "start_at",
        ),
        Index(
            "ix_market_data_gaps_status_detected",
            "status",
            "detected_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    gap_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backfill_job_id: Mapped[str | None] = mapped_column(String(64))


class HistoricalBackfillJobModel(Base):
    """Idempotent audit record for a public historical-data import."""

    __tablename__ = "historical_backfill_jobs"
    __table_args__ = (
        CheckConstraint(
            "start_at <= end_at",
            name="ck_historical_backfill_jobs_time_range",
        ),
        CheckConstraint(
            "retrieved_count >= 0 AND inserted_count >= 0 "
            "AND remaining_gap_count >= 0 AND attempt_count >= 0",
            name="ck_historical_backfill_jobs_counts",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', "
            "'BLOCKED', 'FAILED')",
            name="ck_historical_backfill_jobs_status",
        ),
        CheckConstraint(
            "clock_status IN ('SYNCED', 'WARNING', 'UNSAFE', 'UNKNOWN')",
            name="ck_historical_backfill_jobs_clock_status",
        ),
        Index(
            "ix_historical_backfill_jobs_series_range",
            "exchange",
            "symbol",
            "timeframe",
            "start_at",
            "end_at",
        ),
        Index(
            "ix_historical_backfill_jobs_status_updated",
            "status",
            "updated_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    retrieved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_gap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_hash: Mapped[str | None] = mapped_column(String(64))
    clock_observation_id: Mapped[str | None] = mapped_column(String(64))
    clock_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BackfillQueueItemModel(Base):
    """Durable PostgreSQL work queue with recoverable worker leases."""

    __tablename__ = "backfill_queue_items"
    __table_args__ = (
        CheckConstraint(
            "start_at <= end_at",
            name="ck_backfill_queue_items_time_range",
        ),
        CheckConstraint(
            "max_candles > 0 AND max_candles <= 1000000",
            name="ck_backfill_queue_items_max_candles",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts "
            "AND max_attempts > 0 AND max_attempts <= 100",
            name="ck_backfill_queue_items_attempts",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'LEASED', 'RETRY', 'COMPLETED', "
            "'DEAD_LETTER')",
            name="ck_backfill_queue_items_status",
        ),
        CheckConstraint(
            "(status = 'LEASED' AND leased_by IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'LEASED' AND leased_by IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_backfill_queue_items_lease",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'DEAD_LETTER') "
            "AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED', 'DEAD_LETTER') "
            "AND completed_at IS NULL)",
            name="ck_backfill_queue_items_terminal",
        ),
        Index(
            "ix_backfill_queue_ready",
            "status",
            "available_at",
            "created_at",
            postgresql_where=text(
                "status IN ('PENDING', 'RETRY')"
            ),
        ),
        Index(
            "ix_backfill_queue_expired_leases",
            "lease_expires_at",
            "created_at",
            postgresql_where=text(
                "status = 'LEASED'"
            ),
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    queue_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.historical_backfill_jobs.job_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_candles: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leased_by: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawDataObjectModel(Base):
    """Content-addressed raw payload metadata; bytes live in object storage."""

    __tablename__ = "raw_data_objects"
    __table_args__ = (
        CheckConstraint(
            "uncompressed_bytes > 0 AND stored_bytes > 0",
            name="ck_raw_data_objects_sizes",
        ),
        UniqueConstraint("object_uri", name="uq_raw_data_objects_uri"),
        {"schema": INTERNAL_SCHEMA},
    )

    object_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content_encoding: Mapped[str] = mapped_column(String(32), nullable=False)
    uncompressed_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BackfillRawPageModel(Base):
    """Immutable lineage edge from one provider page to a raw object."""

    __tablename__ = "backfill_raw_pages"
    __table_args__ = (
        CheckConstraint(
            "attempt_count > 0 AND page_index >= 0",
            name="ck_backfill_raw_pages_position",
        ),
        UniqueConstraint(
            "job_id",
            "attempt_count",
            "page_index",
            name="uq_backfill_raw_pages_attempt_page",
        ),
        Index(
            "ix_backfill_raw_pages_job_attempt",
            "job_id",
            "attempt_count",
            "page_index",
        ),
        Index(
            "ix_backfill_raw_pages_object",
            "object_hash",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.historical_backfill_jobs.job_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    object_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.raw_data_objects.object_hash",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    request_params: Mapped[dict] = mapped_column(JsonType, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplayCheckpointModel(Base):
    __tablename__ = "replay_checkpoints"
    __table_args__ = (
        Index(
            "ix_replay_checkpoints_status_updated",
            "status",
            "updated_at",
        ),
    )

    replay_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    consumer_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    next_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[str | None] = mapped_column(String(64))
    events_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MarketCandleModel(Base):
    __tablename__ = "market_candles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    open: Mapped[float] = mapped_column(Numeric, nullable=False)
    high: Mapped[float] = mapped_column(Numeric, nullable=False)
    low: Mapped[float] = mapped_column(Numeric, nullable=False)
    close: Mapped[float] = mapped_column(Numeric, nullable=False)
    volume: Mapped[float] = mapped_column(Numeric, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentOutputModel(Base):
    __tablename__ = "agent_outputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    signal: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JsonType)
    warnings: Mapped[list | None] = mapped_column(JsonType)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionModel(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_action: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    agent_summary: Mapped[list] = mapped_column(JsonType, nullable=False)
    risk_status: Mapped[str] = mapped_column(Text, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskCheckModel(Base):
    __tablename__ = "risk_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    risk_status: Mapped[str] = mapped_column(Text, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    position_size: Mapped[float | None] = mapped_column(Numeric)
    risk_percent: Mapped[float | None] = mapped_column(Numeric)
    stop_loss: Mapped[float | None] = mapped_column(Numeric)
    take_profit: Mapped[float | None] = mapped_column(Numeric)
    risk_reward: Mapped[float | None] = mapped_column(Numeric)
    reason: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperOrderModel(Base):
    __tablename__ = "paper_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    risk_check_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric, nullable=False)
    stop_loss: Mapped[float | None] = mapped_column(Numeric)
    take_profit: Mapped[float | None] = mapped_column(Numeric)
    position_size: Mapped[float] = mapped_column(Numeric, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fees_estimated: Mapped[float | None] = mapped_column(Numeric)
    slippage_estimated: Mapped[float | None] = mapped_column(Numeric)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    audit_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

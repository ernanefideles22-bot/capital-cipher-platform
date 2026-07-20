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


class AgentExecutionJobModel(Base):
    """Durable PAPER-only queue with idempotency and recoverable leases."""

    __tablename__ = "agent_execution_jobs"
    __table_args__ = (
        UniqueConstraint(
            "agent_name",
            "agent_version",
            "idempotency_key",
            name="uq_agent_execution_jobs_idempotency",
        ),
        CheckConstraint(
            "execution_mode = 'PAPER'",
            name="ck_agent_execution_jobs_paper_only",
        ),
        CheckConstraint(
            "decision_role IN ('PRIMARY', 'SHADOW')",
            name="ck_agent_execution_jobs_decision_role",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'LEASED', 'RETRY', 'COMPLETED', "
            "'DEAD_LETTER')",
            name="ck_agent_execution_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts "
            "AND max_attempts > 0 AND max_attempts <= 10",
            name="ck_agent_execution_jobs_attempts",
        ),
        CheckConstraint(
            "(status = 'LEASED' AND leased_by IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'LEASED' AND leased_by IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_agent_execution_jobs_lease",
        ),
        CheckConstraint(
            "(status IN ('COMPLETED', 'DEAD_LETTER') "
            "AND completed_at IS NOT NULL) OR "
            "(status NOT IN ('COMPLETED', 'DEAD_LETTER') "
            "AND completed_at IS NULL)",
            name="ck_agent_execution_jobs_terminal",
        ),
        Index(
            "ix_agent_execution_jobs_ready",
            "status",
            "available_at",
            "created_at",
            postgresql_where=text("status IN ('PENDING', 'RETRY')"),
        ),
        Index(
            "ix_agent_execution_jobs_expired_leases",
            "lease_expires_at",
            "created_at",
            postgresql_where=text("status = 'LEASED'"),
        ),
        Index(
            "ix_agent_execution_jobs_correlation",
            "correlation_id",
            "created_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime_version: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_role: Mapped[str] = mapped_column(String(16), nullable=False)
    critical: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    leased_by: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    output_payload: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class AgentExecutionAttemptModel(Base):
    """Append-only attempt evidence for an agent execution."""

    __tablename__ = "agent_execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "attempt_number",
            name="uq_agent_execution_attempts_number",
        ),
        CheckConstraint(
            "attempt_number > 0 AND attempt_number <= 10",
            name="ck_agent_execution_attempts_number",
        ),
        CheckConstraint(
            "completed_at >= started_at",
            name="ck_agent_execution_attempts_time",
        ),
        Index(
            "ix_agent_execution_attempts_execution",
            "execution_id",
            "attempt_number",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    row_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    execution_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.agent_execution_jobs.execution_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    output_payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class AgentMemoryEntryModel(Base):
    """Append-only execution-scoped memory; agents never query it directly."""

    __tablename__ = "agent_memory_entries"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_agent_memory_entries_sequence",
        ),
        CheckConstraint(
            "sequence > 0",
            name="ck_agent_memory_entries_sequence",
        ),
        CheckConstraint(
            "entry_type IN ('INPUT', 'ATTEMPT', 'OUTPUT', 'DEAD_LETTER')",
            name="ck_agent_memory_entries_type",
        ),
        Index(
            "ix_agent_memory_entries_execution",
            "execution_id",
            "sequence",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    row_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        Identity(always=True),
        primary_key=True,
    )
    execution_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.agent_execution_jobs.execution_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SpecialistEvidenceModel(Base):
    """Append-only normalized evidence for external-data specialists."""

    __tablename__ = "specialist_evidence"
    __table_args__ = (
        CheckConstraint(
            "domain IN ('DERIVATIVES', 'MACRO', 'ONCHAIN', 'NEWS')",
            name="ck_specialist_evidence_domain",
        ),
        CheckConstraint(
            "quality_score >= 0 AND quality_score <= 100",
            name="ck_specialist_evidence_quality",
        ),
        CheckConstraint(
            "received_at >= observed_at",
            name="ck_specialist_evidence_time",
        ),
        UniqueConstraint(
            "source",
            "source_event_id",
            name="uq_specialist_evidence_source_event",
        ),
        Index(
            "ix_specialist_evidence_lookup",
            "domain",
            "metric_name",
            "scope",
            "observed_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    domain: Mapped[str] = mapped_column(String(16), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_score: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    provenance_uri: Mapped[str | None] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentForecastModel(Base):
    """Append-only PAPER forecast captured before its target horizon."""

    __tablename__ = "agent_forecasts"
    __table_args__ = (
        CheckConstraint(
            "signal IN ('BUY', 'SELL', 'HOLD', 'WAIT', 'BLOCK', 'NEUTRAL')",
            name="ck_agent_forecast_signal",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_agent_forecast_confidence",
        ),
        CheckConstraint(
            "probability_up >= 0 AND probability_up <= 1",
            name="ck_agent_forecast_probability",
        ),
        CheckConstraint(
            "decision_role IN ('PRIMARY', 'SHADOW')",
            name="ck_agent_forecast_role",
        ),
        CheckConstraint(
            "reference_price > 0 AND horizon_seconds > 0 "
            "AND target_at > forecast_at",
            name="ck_agent_forecast_horizon",
        ),
        Index(
            "ix_agent_forecasts_pending",
            "symbol",
            "timeframe",
            "target_at",
        ),
        Index(
            "ix_agent_forecasts_agent",
            "agent_name",
            "agent_version",
            "forecast_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    forecast_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    probability_up: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    reference_price: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    forecast_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    target_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    horizon_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class AgentForecastOutcomeModel(Base):
    """Append-only realized result and marginal contribution."""

    __tablename__ = "agent_forecast_outcomes"
    __table_args__ = (
        CheckConstraint(
            "realized_price > 0",
            name="ck_agent_forecast_outcome_price",
        ),
        CheckConstraint(
            "realized_up >= 0 AND realized_up <= 1",
            name="ck_agent_forecast_outcome_realized",
        ),
        CheckConstraint(
            "brier_loss >= 0 AND brier_loss <= 1 "
            "AND ensemble_brier_loss >= 0 AND ensemble_brier_loss <= 1 "
            "AND leave_one_out_brier_loss >= 0 "
            "AND leave_one_out_brier_loss <= 1",
            name="ck_agent_forecast_outcome_losses",
        ),
        CheckConstraint(
            "marginal_contribution >= -1 "
            "AND marginal_contribution <= 1",
            name="ck_agent_forecast_outcome_contribution",
        ),
        CheckConstraint(
            "cohort_size >= 1",
            name="ck_agent_forecast_outcome_cohort",
        ),
        Index(
            "ix_agent_forecast_outcomes_realized",
            "realized_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    outcome_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    forecast_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.agent_forecasts.forecast_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    realized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    realized_price: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    realized_return: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    realized_up: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    correct: Mapped[bool | None] = mapped_column(Boolean)
    brier_loss: Mapped[float] = mapped_column(Numeric(20, 18), nullable=False)
    ensemble_probability_up: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    ensemble_brier_loss: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    leave_one_out_probability_up: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    leave_one_out_brier_loss: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    marginal_contribution: Mapped[float] = mapped_column(
        Numeric(20, 18),
        nullable=False,
    )
    cohort_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ConsensusExperimentModel(Base):
    """Immutable versioned performance-weighting policy."""

    __tablename__ = "consensus_experiments"
    __table_args__ = (
        UniqueConstraint(
            "name",
            "version",
            name="uq_consensus_experiment_name_version",
        ),
        CheckConstraint(
            "mode IN ('SHADOW', 'CONFIRMATION')",
            name="ck_consensus_experiment_mode",
        ),
        Index(
            "ix_consensus_experiments_created",
            "created_at",
            "experiment_id",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ConsensusExperimentEventModel(Base):
    """Append-only activation/retirement evidence."""

    __tablename__ = "consensus_experiment_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('CREATED', 'ACTIVATED', 'RETIRED')",
            name="ck_consensus_experiment_event_type",
        ),
        Index(
            "ix_consensus_experiment_events_experiment_created",
            "experiment_id",
            "created_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.consensus_experiments.experiment_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class WeightedConsensusModel(Base):
    """Append-only consensus snapshot."""

    __tablename__ = "weighted_consensus_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('INSUFFICIENT_DATA', 'READY')",
            name="ck_weighted_consensus_status",
        ),
        Index(
            "ix_weighted_consensus_symbol_created",
            "symbol",
            "timeframe",
            "created_at",
        ),
        Index(
            "ix_weighted_consensus_experiment_created",
            "experiment_id",
            "created_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    consensus_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.consensus_experiments.experiment_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    eligible_agent_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class DriftObservationModel(Base):
    """Append-only rolling drift evidence for one agent version."""

    __tablename__ = "drift_observations"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('NONE', 'WARNING', 'CRITICAL')",
            name="ck_drift_observation_severity",
        ),
        Index(
            "ix_drift_observations_agent_observed",
            "agent_name",
            "agent_version",
            "observed_at",
        ),
        Index(
            "ix_drift_observations_experiment_severity",
            "experiment_id",
            "severity",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.consensus_experiments.experiment_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class PortfolioProposalModel(Base):
    """Append-only advisory construction artifact."""

    __tablename__ = "portfolio_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NO_ACTION', 'PROPOSED', 'BLOCKED')",
            name="ck_portfolio_proposal_status",
        ),
        Index(
            "ix_portfolio_proposals_symbol_created",
            "symbol",
            "timeframe",
            "created_at",
        ),
        Index(
            "ix_portfolio_proposals_consensus",
            "consensus_id",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    consensus_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.weighted_consensus_snapshots.consensus_id",
            ondelete="RESTRICT",
        ),
    )
    experiment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.consensus_experiments.experiment_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    max_notional: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class OperationalMetricSnapshotModel(Base):
    """Append-only bounded operational metric materialization."""

    __tablename__ = "operational_metric_snapshots"
    __table_args__ = (
        CheckConstraint(
            "registered_agents >= 0 AND active_agents >= 0 "
            "AND active_agents <= registered_agents",
            name="ck_operational_metric_snapshot_agents",
        ),
        Index(
            "ix_operational_metric_snapshots_captured",
            "captured_at",
            "snapshot_id",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_agents: Mapped[int] = mapped_column(Integer, nullable=False)
    active_agents: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SLOEvaluationModel(Base):
    """Append-only service-level objective evidence."""

    __tablename__ = "slo_evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NO_DATA', 'HEALTHY', 'WARNING', 'BREACHED')",
            name="ck_slo_evaluation_status",
        ),
        CheckConstraint(
            "sample_count >= 0",
            name="ck_slo_evaluation_samples",
        ),
        Index(
            "ix_slo_evaluations_name_evaluated",
            "slo_name",
            "evaluated_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    slo_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class OperationalAlertEventModel(Base):
    """Append-only OPENED/RESOLVED alert lifecycle."""

    __tablename__ = "operational_alert_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('OPENED', 'RESOLVED')",
            name="ck_operational_alert_event_type",
        ),
        CheckConstraint(
            "severity IN ('WARNING', 'ERROR', 'CRITICAL')",
            name="ck_operational_alert_severity",
        ),
        CheckConstraint(
            "lifecycle_sequence >= 1",
            name="ck_operational_alert_sequence",
        ),
        UniqueConstraint(
            "alert_key",
            "lifecycle_sequence",
            name="uq_operational_alert_lifecycle",
        ),
        Index(
            "ix_operational_alert_events_key_occurred",
            "alert_key",
            "occurred_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    alert_event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    alert_key: Mapped[str] = mapped_column(String(160), nullable=False)
    lifecycle_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class CostUsageRecordModel(Base):
    """Append-only operational cost attribution."""

    __tablename__ = "cost_usage_records"
    __table_args__ = (
        CheckConstraint(
            "cost_center IN ('AGENT_RUNTIME', 'EXTERNAL_DATA', "
            "'STORAGE', 'OBSERVABILITY')",
            name="ck_cost_usage_center",
        ),
        CheckConstraint(
            "estimated_cost_usd >= 0",
            name="ck_cost_usage_nonnegative",
        ),
        Index(
            "ix_cost_usage_records_center_observed",
            "cost_center",
            "observed_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    cost_center: Mapped[str] = mapped_column(String(32), nullable=False)
    resource: Mapped[str] = mapped_column(String(128), nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(
        Numeric(20, 8),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ResilienceTestRunModel(Base):
    """Append-only load, chaos and recovery acceptance evidence."""

    __tablename__ = "resilience_test_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN ('LOAD', 'CHAOS', 'RECOVERY')",
            name="ck_resilience_test_run_type",
        ),
        CheckConstraint(
            "status IN ('PASSED', 'FAILED')",
            name="ck_resilience_test_run_status",
        ),
        Index(
            "ix_resilience_test_runs_type_completed",
            "run_type",
            "completed_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    run_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scenario: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


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


class RiskEvaluationModel(Base):
    """Immutable central-risk evidence; one row per idempotent request."""

    __tablename__ = "risk_evaluations"
    __table_args__ = (
        CheckConstraint("length(evaluation_id) = 64", name="ck_risk_evaluation_id"),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_risk_evaluation_fingerprint",
        ),
        CheckConstraint(
            "risk_status IN ('APPROVED', 'REDUCED', 'BLOCKED', 'KILL_SWITCH')",
            name="ck_risk_evaluation_status",
        ),
        CheckConstraint(
            "approved = (risk_status IN ('APPROVED', 'REDUCED'))",
            name="ck_risk_evaluation_approval",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_risk_evaluations_idempotency_key",
        ),
        Index(
            "ix_risk_evaluations_decision_created",
            "decision_id",
            "created_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    evaluation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    risk_check_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    risk_status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderApprovalModel(Base):
    """Single-use execution capability minted by the central risk engine."""

    __tablename__ = "order_approvals"
    __table_args__ = (
        CheckConstraint("length(approval_id) = 64", name="ck_order_approval_id"),
        CheckConstraint(
            "length(position_snapshot_hash) = 64",
            name="ck_order_approval_position_snapshot",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED')",
            name="ck_order_approval_status",
        ),
        CheckConstraint("max_notional > 0", name="ck_order_approval_notional"),
        CheckConstraint("max_leverage >= 1", name="ck_order_approval_leverage"),
        CheckConstraint("reference_price > 0", name="ck_order_approval_price"),
        CheckConstraint(
            "max_entry_deviation_bps >= 0",
            name="ck_order_approval_deviation",
        ),
        CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_order_approval_side",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_order_approval_expiry",
        ),
        CheckConstraint(
            "(status = 'CONSUMED' AND consumed_at IS NOT NULL "
            "AND ((paper_order_id IS NOT NULL AND oms_order_id IS NULL) "
            "OR (paper_order_id IS NULL AND oms_order_id IS NOT NULL))) OR "
            "(status <> 'CONSUMED' AND consumed_at IS NULL "
            "AND paper_order_id IS NULL AND oms_order_id IS NULL)",
            name="ck_order_approval_terminal",
        ),
        UniqueConstraint(
            "evaluation_id",
            name="uq_order_approvals_evaluation",
        ),
        Index(
            "ix_order_approvals_active_expiry",
            "expires_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.risk_evaluations.evaluation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    risk_check_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    position_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    max_notional: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    max_leverage: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    reference_price: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    max_entry_deviation_bps: Mapped[float] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paper_order_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    oms_order_id: Mapped[str | None] = mapped_column(String(36), unique=True)


class RiskControlStateModel(Base):
    __tablename__ = "risk_control_state"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_risk_control_singleton"),
        CheckConstraint("revision >= 0", name="ck_risk_control_revision"),
        {"schema": INTERNAL_SCHEMA},
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RiskControlEventModel(Base):
    __tablename__ = "risk_control_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('TRIGGERED', 'RESET')",
            name="ck_risk_control_event_type",
        ),
        CheckConstraint("revision > 0", name="ck_risk_control_event_revision"),
        UniqueConstraint("revision", name="uq_risk_control_event_revision"),
        Index("ix_risk_control_events_created", "created_at"),
        {"schema": INTERNAL_SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperOrderModel(Base):
    __tablename__ = "paper_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    risk_check_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text, nullable=False, default="UNSPECIFIED")
    side: Mapped[str] = mapped_column(Text, nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric, nullable=False)
    stop_loss: Mapped[float | None] = mapped_column(Numeric)
    take_profit: Mapped[float | None] = mapped_column(Numeric)
    position_size: Mapped[float] = mapped_column(Numeric, nullable=False)
    leverage: Mapped[float] = mapped_column(Numeric, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    fees_estimated: Mapped[float | None] = mapped_column(Numeric)
    slippage_estimated: Mapped[float | None] = mapped_column(Numeric)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pnl: Mapped[float | None] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OMSOrderModel(Base):
    """Durable OMS identity and current state."""

    __tablename__ = "oms_orders"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_oms_order_environment",
        ),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_oms_order_exchange",
        ),
        CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_oms_order_side",
        ),
        CheckConstraint(
            "order_type IN ('MARKET', 'LIMIT')",
            name="ck_oms_order_type",
        ),
        CheckConstraint(
            "time_in_force IN ('GTC', 'IOC', 'FOK', 'POST_ONLY')",
            name="ck_oms_order_time_in_force",
        ),
        CheckConstraint(
            "status IN ('CREATED', 'PENDING_SUBMISSION', 'SUBMITTED', "
            "'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELED', "
            "'REJECTED', 'EXPIRED', 'UNKNOWN', 'QUARANTINED')",
            name="ck_oms_order_status",
        ),
        CheckConstraint("quantity > 0", name="ck_oms_order_quantity"),
        CheckConstraint(
            "requested_notional > 0",
            name="ck_oms_order_notional",
        ),
        CheckConstraint("reference_price > 0", name="ck_oms_order_reference"),
        CheckConstraint("leverage >= 1", name="ck_oms_order_leverage"),
        CheckConstraint("state_version >= 1", name="ck_oms_order_version"),
        CheckConstraint(
            "cumulative_filled_quantity >= 0 "
            "AND cumulative_filled_quantity <= quantity",
            name="ck_oms_order_filled_quantity",
        ),
        CheckConstraint(
            "(order_type = 'LIMIT' AND limit_price IS NOT NULL) "
            "OR (order_type = 'MARKET')",
            name="ck_oms_order_limit_price",
        ),
        CheckConstraint(
            "(status IN ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', "
            "'QUARANTINED') AND terminal_at IS NOT NULL) OR "
            "(status NOT IN ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', "
            "'QUARANTINED') AND terminal_at IS NULL)",
            name="ck_oms_order_terminal",
        ),
        UniqueConstraint(
            "exchange",
            "environment",
            "client_order_id",
            name="uq_oms_order_client_identity",
        ),
        Index(
            "ix_oms_orders_active_updated",
            "exchange",
            "environment",
            "updated_at",
            postgresql_where=text(
                "status IN ('CREATED', 'PENDING_SUBMISSION', 'SUBMITTED', "
                "'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN')"
            ),
        ),
        Index("ix_oms_orders_venue_order", "venue_order_id"),
        {"schema": INTERNAL_SCHEMA},
    )

    oms_order_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    risk_check_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approval_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.order_approvals.approval_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    timeframe: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    requested_notional: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    leverage: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Numeric(38, 18))
    reference_price: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    venue_order_id: Mapped[str | None] = mapped_column(Text)
    cumulative_filled_quantity: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
        default=0,
    )
    average_fill_price: Mapped[float | None] = mapped_column(Numeric(38, 18))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OMSOrderEventModel(Base):
    """Append-only state transition evidence."""

    __tablename__ = "oms_order_events"
    __table_args__ = (
        UniqueConstraint(
            "oms_order_id",
            "state_version",
            name="uq_oms_order_event_version",
        ),
        Index("ix_oms_order_events_order_created", "oms_order_id", "created_at"),
        {"schema": INTERNAL_SCHEMA},
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    oms_order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.oms_orders.oms_order_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ExecutionCommandModel(Base):
    """Transactional TESTNET submission/cancellation outbox."""

    __tablename__ = "execution_commands"
    __table_args__ = (
        CheckConstraint(
            "command_type IN ('SUBMIT', 'CANCEL')",
            name="ck_execution_command_type",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'LEASED', 'COMPLETED', 'DEAD_LETTER')",
            name="ck_execution_command_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts = 1 "
            "AND attempt_count <= max_attempts",
            name="ck_execution_command_attempts",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND leased_by IS NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'LEASED' AND leased_by IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('COMPLETED', 'DEAD_LETTER') "
            "AND leased_by IS NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_execution_command_lifecycle",
        ),
        UniqueConstraint(
            "oms_order_id",
            "command_type",
            name="uq_execution_command_order_type",
        ),
        Index(
            "ix_execution_commands_claimable",
            "available_at",
            "created_at",
            postgresql_where=text(
                "status = 'PENDING' OR "
                "(status = 'LEASED' AND lease_expires_at IS NOT NULL)"
            ),
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    oms_order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.oms_orders.oms_order_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    leased_by: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_error_type: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionFillModel(Base):
    """Idempotent venue fill evidence."""

    __tablename__ = "execution_fills"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_execution_fill_quantity"),
        CheckConstraint("price > 0", name="ck_execution_fill_price"),
        CheckConstraint("fee >= 0", name="ck_execution_fill_fee"),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_execution_fill_exchange",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_execution_fill_environment",
        ),
        CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_execution_fill_side",
        ),
        Index("ix_execution_fills_order_time", "oms_order_id", "occurred_at"),
        Index("ix_execution_fills_venue_order", "venue_order_id"),
        {"schema": INTERNAL_SCHEMA},
    )

    fill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    oms_order_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.oms_orders.oms_order_id",
            ondelete="RESTRICT",
        ),
    )
    venue_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String(36))
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    fee_asset: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class ReconciliationRunModel(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('MATCHED', 'DRIFT', 'FAILED')",
            name="ck_reconciliation_run_status",
        ),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_reconciliation_run_exchange",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_reconciliation_run_environment",
        ),
        CheckConstraint(
            "local_order_count >= 0 AND venue_order_count >= 0 "
            "AND fill_count >= 0 AND position_count >= 0 "
            "AND balance_count >= 0 AND mismatch_count >= 0 "
            "AND critical_mismatch_count >= 0 "
            "AND critical_mismatch_count <= mismatch_count",
            name="ck_reconciliation_run_counts",
        ),
        CheckConstraint(
            "(status = 'FAILED' AND error_type IS NOT NULL) OR "
            "(status <> 'FAILED' AND error_type IS NULL)",
            name="ck_reconciliation_run_error",
        ),
        Index(
            "ix_reconciliation_runs_venue_completed",
            "exchange",
            "environment",
            "completed_at",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    local_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    venue_order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False)
    position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    critical_mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    error_type: Mapped[str | None] = mapped_column(String(128))


class ReconciliationMismatchModel(Base):
    __tablename__ = "reconciliation_mismatches"
    __table_args__ = (
        CheckConstraint(
            "mismatch_type IN ("
            "'LOCAL_ORDER_MISSING_AT_VENUE', 'ORPHAN_VENUE_ORDER', "
            "'ORPHAN_VENUE_FILL', "
            "'ORDER_STATUS_DRIFT', 'FILLED_QUANTITY_DRIFT', "
            "'POSITION_QUANTITY_DRIFT', 'ADAPTER_UNAVAILABLE')",
            name="ck_reconciliation_mismatch_type",
        ),
        CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'CRITICAL')",
            name="ck_reconciliation_mismatch_severity",
        ),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_reconciliation_mismatch_exchange",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_reconciliation_mismatch_environment",
        ),
        Index(
            "ix_reconciliation_mismatches_run_severity",
            "run_id",
            "severity",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    mismatch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.reconciliation_runs.run_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    mismatch_type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    oms_order_id: Mapped[str | None] = mapped_column(String(36))
    venue_order_id: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    expected: Mapped[dict] = mapped_column(JsonType, nullable=False)
    observed: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class VenuePositionSnapshotModel(Base):
    __tablename__ = "venue_position_snapshots"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_venue_position_quantity"),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_venue_position_exchange",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_venue_position_environment",
        ),
        CheckConstraint(
            "side IN ('BUY', 'SELL')",
            name="ck_venue_position_side",
        ),
        Index(
            "ix_venue_position_snapshots_run",
            "run_id",
            "symbol",
            "side",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.reconciliation_runs.run_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Numeric(38, 18))
    mark_price: Mapped[float | None] = mapped_column(Numeric(38, 18))
    unrealized_pnl: Mapped[float] = mapped_column(
        Numeric(38, 18),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class VenueBalanceSnapshotModel(Base):
    __tablename__ = "venue_balance_snapshots"
    __table_args__ = (
        CheckConstraint(
            "available >= 0 AND locked >= 0 AND equity >= 0",
            name="ck_venue_balance_nonnegative",
        ),
        CheckConstraint(
            "exchange IN ('BINANCE', 'BYBIT')",
            name="ck_venue_balance_exchange",
        ),
        CheckConstraint(
            "environment IN ('PAPER', 'TESTNET')",
            name="ck_venue_balance_environment",
        ),
        Index(
            "ix_venue_balance_snapshots_run_asset",
            "run_id",
            "asset",
        ),
        {"schema": INTERNAL_SCHEMA},
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            f"{INTERNAL_SCHEMA}.reconciliation_runs.run_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    asset: Mapped[str] = mapped_column(Text, nullable=False)
    available: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    locked: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    equity: Mapped[float] = mapped_column(Numeric(38, 18), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    audit_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

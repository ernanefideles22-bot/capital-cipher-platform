"""Application configuration.

Follows docs/16-security-rules.md and docs/21-deployment.md:
- no secrets in code, everything from environment variables;
- Phase 1 system mode defaults to PAPER;
- LIVE-related settings intentionally do not exist.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PHASE_1_ALLOWED_MODES: tuple[str, ...] = ("OFFLINE", "PAPER")


class Settings(BaseSettings):
    """Runtime settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="local", alias="APP_ENV")
    app_name: str = "capital-cipher-api"
    app_version: str = "0.7.0"

    system_mode: str = Field(default="PAPER", alias="SYSTEM_MODE")

    database_url: str = Field(
        default="sqlite+aiosqlite:///./capital_cipher.db", alias="DATABASE_URL"
    )
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    event_broker_required: bool = Field(default=False, alias="EVENT_BROKER_REQUIRED")
    redis_stream_prefix: str = Field(
        default="capital-cipher",
        alias="REDIS_STREAM_PREFIX",
        pattern=r"^[a-z0-9][a-z0-9:-]{1,63}$",
    )
    redis_stream_max_length: int = Field(
        default=100_000,
        alias="REDIS_STREAM_MAX_LENGTH",
        ge=1_000,
        le=10_000_000,
    )
    broker_max_message_bytes: int = Field(
        default=1_000_000,
        alias="BROKER_MAX_MESSAGE_BYTES",
        ge=1_024,
        le=10_000_000,
    )
    outbox_poll_interval_seconds: float = Field(
        default=1.0,
        alias="OUTBOX_POLL_INTERVAL_SECONDS",
        ge=0.1,
        le=60.0,
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    cors_allowed_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ALLOWED_ORIGINS",
    )
    api_rate_limit_per_minute: int = Field(
        default=300, alias="API_RATE_LIMIT_PER_MINUTE", ge=1, le=100_000
    )
    max_request_body_bytes: int = Field(
        default=2_000_000, alias="MAX_REQUEST_BODY_BYTES", ge=1, le=20_000_000
    )

    allowed_symbols: str = Field(default="BTCUSDT,ETHUSDT,SOLUSDT", alias="ALLOWED_SYMBOLS")
    default_timeframe: str = Field(default="15m", alias="DEFAULT_TIMEFRAME")
    default_exchange: str = Field(default="BINANCE", alias="DEFAULT_EXCHANGE")

    # API auth for sensitive endpoints (docs/13, docs/16). Loaded from env, never hardcoded.
    admin_api_key: str | None = Field(default=None, alias="ADMIN_API_KEY")

    # Risk limits for paper trading (docs/06-risk-management.md).
    risk_per_trade_percent: float = Field(default=1.0, alias="RISK_PER_TRADE_PERCENT")
    max_daily_drawdown_percent: float = Field(default=5.0, alias="MAX_DAILY_DRAWDOWN_PERCENT")
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")
    max_open_positions: int = Field(default=3, alias="MAX_OPEN_POSITIONS")
    default_leverage: float = Field(default=1.0, alias="DEFAULT_LEVERAGE")
    max_leverage_simulated: float = Field(default=5.0, alias="MAX_LEVERAGE_SIMULATED")

    # Paper trading account (simulated balance only — no real money, docs/18).
    paper_initial_balance: float = Field(default=10_000.0, alias="PAPER_INITIAL_BALANCE")
    fee_rate_percent: float = Field(default=0.08, alias="FEE_RATE_PERCENT")
    slippage_rate_percent: float = Field(default=0.02, alias="SLIPPAGE_RATE_PERCENT")

    # Decision engine (docs/25-decision-engine.md).
    minimum_candidate_confidence: int = Field(default=70, alias="MINIMUM_CANDIDATE_CONFIDENCE")

    # Data quality (docs/32-data-quality.md).
    max_market_data_delay_ms: int = Field(default=5000, alias="MAX_MARKET_DATA_DELAY_MS")
    min_data_quality_score: int = Field(default=60, alias="MIN_DATA_QUALITY_SCORE")

    # Agent execution (docs/28-agent-lifecycle.md).
    agent_timeout_ms: int = Field(default=5000, alias="AGENT_TIMEOUT_MS")

    @field_validator("system_mode")
    @classmethod
    def validate_phase_1_mode(cls, value: str) -> str:
        """Phase 1 hard rule: only OFFLINE and PAPER are acceptable boot modes.

        docs/16-security-rules.md — any attempt to configure LIVE/LIVE_LOCKED in
        Phase 1 is a SecurityError-level misconfiguration and must fail fast.
        """
        mode = value.strip().upper()
        if mode not in PHASE_1_ALLOWED_MODES:
            raise ValueError(
                f"SYSTEM_MODE '{mode}' is not allowed in Phase 1. "
                f"Allowed modes: {PHASE_1_ALLOWED_MODES}. Live trading is prohibited."
            )
        return mode

    @field_validator("admin_api_key")
    @classmethod
    def validate_admin_api_key(cls, value: str | None) -> str | None:
        """Configured administrator keys must have enough entropy.

        An empty value keeps every protected endpoint locked. Short placeholder
        values are rejected at startup so they cannot accidentally reach a
        shared environment.
        """
        if value is None or not value.strip():
            return None
        candidate = value.strip()
        if len(candidate) < 32:
            raise ValueError("ADMIN_API_KEY must contain at least 32 characters")
        return candidate

    @model_validator(mode="after")
    def validate_event_broker_configuration(self) -> "Settings":
        if self.event_broker_required and not self.redis_url:
            raise ValueError("REDIS_URL is required when EVENT_BROKER_REQUIRED is enabled")
        return self

    @property
    def allowed_symbols_list(self) -> list[str]:
        return [s.strip().upper() for s in self.allowed_symbols.split(",") if s.strip()]

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

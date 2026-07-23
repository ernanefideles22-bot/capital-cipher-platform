"""Application configuration.

Follows docs/16-security-rules.md and docs/21-deployment.md:
- no secrets in code, everything from environment variables;
- system mode defaults to PAPER;
- OMS can target PAPER or an explicitly acknowledged TESTNET;
- LIVE-related settings intentionally do not exist.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PHASE_1_ALLOWED_MODES: tuple[str, ...] = ("OFFLINE", "PAPER")


class Settings(BaseSettings):
    """Runtime settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="local", alias="APP_ENV")
    app_name: str = "capital-cipher-api"
    app_version: str = "0.26.0"

    system_mode: str = Field(default="PAPER", alias="SYSTEM_MODE")
    oms_execution_environment: str = Field(
        default="PAPER",
        alias="OMS_EXECUTION_ENVIRONMENT",
    )
    oms_testnet_enabled: bool = Field(
        default=False,
        alias="OMS_TESTNET_ENABLED",
    )
    oms_testnet_acknowledgement: str | None = Field(
        default=None,
        alias="OMS_TESTNET_ACKNOWLEDGEMENT",
    )
    oms_testnet_exchange: str = Field(
        default="BINANCE",
        alias="OMS_TESTNET_EXCHANGE",
    )
    binance_testnet_rest_url: str = Field(
        default="https://testnet.binance.vision",
        alias="BINANCE_TESTNET_REST_URL",
    )
    bybit_testnet_rest_url: str = Field(
        default="https://api-testnet.bybit.com",
        alias="BYBIT_TESTNET_REST_URL",
    )
    bybit_testnet_category: str = Field(
        default="linear",
        alias="BYBIT_TESTNET_CATEGORY",
    )
    oms_http_timeout_seconds: float = Field(
        default=5.0,
        alias="OMS_HTTP_TIMEOUT_SECONDS",
        gt=0,
        le=30,
    )
    oms_receive_window_ms: int = Field(
        default=5_000,
        alias="OMS_RECEIVE_WINDOW_MS",
        ge=1_000,
        le=5_000,
    )
    oms_worker_enabled: bool = Field(
        default=True,
        alias="OMS_WORKER_ENABLED",
    )
    oms_worker_poll_interval_seconds: float = Field(
        default=0.25,
        alias="OMS_WORKER_POLL_INTERVAL_SECONDS",
        gt=0,
        le=60,
    )
    oms_command_lease_seconds: float = Field(
        default=15.0,
        alias="OMS_COMMAND_LEASE_SECONDS",
        ge=1,
        le=300,
    )
    oms_reconciliation_enabled: bool = Field(
        default=True,
        alias="OMS_RECONCILIATION_ENABLED",
    )
    oms_reconciliation_interval_seconds: float = Field(
        default=30.0,
        alias="OMS_RECONCILIATION_INTERVAL_SECONDS",
        ge=1,
        le=3_600,
    )
    oms_halt_on_critical_drift: bool = Field(
        default=True,
        alias="OMS_HALT_ON_CRITICAL_DRIFT",
    )

    database_url: str = Field(
        default="sqlite+aiosqlite:///./capital_cipher.db", alias="DATABASE_URL"
    )
    database_pool_size: int = Field(
        default=5,
        alias="DATABASE_POOL_SIZE",
        ge=1,
        le=50,
    )
    database_max_overflow: int = Field(
        default=0,
        alias="DATABASE_MAX_OVERFLOW",
        ge=0,
        le=50,
    )
    database_pool_timeout_seconds: float = Field(
        default=15,
        alias="DATABASE_POOL_TIMEOUT_SECONDS",
        ge=1,
        le=300,
    )
    database_pool_recycle_seconds: int = Field(
        default=300,
        alias="DATABASE_POOL_RECYCLE_SECONDS",
        ge=30,
        le=86_400,
    )
    redis_url: str | None = Field(default=None, alias="REDIS_URL")
    event_broker_required: bool = Field(default=False, alias="EVENT_BROKER_REQUIRED")
    enable_market_data: bool = Field(default=False, alias="ENABLE_MARKET_DATA")
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
    operations_monitor_enabled: bool = Field(
        default=False,
        alias="OPERATIONS_MONITOR_ENABLED",
    )
    operations_monitor_interval_seconds: float = Field(
        default=30.0,
        alias="OPERATIONS_MONITOR_INTERVAL_SECONDS",
        ge=1,
        le=3_600,
    )
    operations_metric_capacity: int = Field(
        default=10_000,
        alias="OPERATIONS_METRIC_CAPACITY",
        ge=100,
        le=1_000_000,
    )
    operations_window_seconds: int = Field(
        default=300,
        alias="OPERATIONS_WINDOW_SECONDS",
        ge=10,
        le=86_400,
    )
    operations_daily_budget_usd: float = Field(
        default=10.0,
        alias="OPERATIONS_DAILY_BUDGET_USD",
        gt=0,
        le=1_000_000,
    )
    operations_budget_warning_percent: float = Field(
        default=80.0,
        alias="OPERATIONS_BUDGET_WARNING_PERCENT",
        gt=0,
        lt=100,
    )
    agent_execution_unit_cost_usd: float = Field(
        default=0.0,
        alias="AGENT_EXECUTION_UNIT_COST_USD",
        ge=0,
        le=1_000,
    )
    agent_success_slo: float = Field(
        default=0.99,
        alias="AGENT_SUCCESS_SLO",
        ge=0.5,
        le=1,
    )
    agent_p95_latency_slo_ms: float = Field(
        default=2_000,
        alias="AGENT_P95_LATENCY_SLO_MS",
        gt=0,
        le=300_000,
    )
    orchestrator_success_slo: float = Field(
        default=0.99,
        alias="ORCHESTRATOR_SUCCESS_SLO",
        ge=0.5,
        le=1,
    )
    orchestrator_p95_latency_slo_ms: float = Field(
        default=5_000,
        alias="ORCHESTRATOR_P95_LATENCY_SLO_MS",
        gt=0,
        le=300_000,
    )
    recovery_successes_required: int = Field(
        default=3,
        alias="RECOVERY_SUCCESSES_REQUIRED",
        ge=2,
        le=100,
    )
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
    max_total_drawdown_percent: float = Field(
        default=10.0,
        alias="MAX_TOTAL_DRAWDOWN_PERCENT",
        gt=0,
        le=100,
    )
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")
    max_open_positions: int = Field(default=3, alias="MAX_OPEN_POSITIONS")
    default_leverage: float = Field(
        default=1.0,
        alias="DEFAULT_LEVERAGE",
        ge=1,
        le=125,
    )
    max_leverage_simulated: float = Field(
        default=5.0,
        alias="MAX_LEVERAGE_SIMULATED",
        ge=1,
        le=125,
    )
    max_gross_exposure_percent: float = Field(
        default=200.0, alias="MAX_GROSS_EXPOSURE_PERCENT", gt=0, le=2_000
    )
    max_net_exposure_percent: float = Field(
        default=150.0, alias="MAX_NET_EXPOSURE_PERCENT", gt=0, le=2_000
    )
    max_symbol_exposure_percent: float = Field(
        default=100.0, alias="MAX_SYMBOL_EXPOSURE_PERCENT", gt=0, le=2_000
    )
    max_strategy_exposure_percent: float = Field(
        default=100.0, alias="MAX_STRATEGY_EXPOSURE_PERCENT", gt=0, le=2_000
    )
    max_single_position_percent: float = Field(
        default=100.0, alias="MAX_SINGLE_POSITION_PERCENT", gt=0, le=1_000
    )
    max_symbol_concentration_percent: float = Field(
        default=90.0,
        alias="MAX_SYMBOL_CONCENTRATION_PERCENT",
        gt=0,
        le=100,
    )
    max_portfolio_var_percent: float = Field(
        default=5.0, alias="MAX_PORTFOLIO_VAR_PERCENT", gt=0, le=100
    )
    var_confidence: float = Field(
        default=0.99, alias="VAR_CONFIDENCE", ge=0.90, lt=1
    )
    var_lookback: int = Field(
        default=100, alias="VAR_LOOKBACK", ge=20, le=2_000
    )
    var_min_observations: int = Field(
        default=30, alias="VAR_MIN_OBSERVATIONS", ge=10, le=1_000
    )
    fallback_volatility_percent: float = Field(
        default=1.0,
        alias="FALLBACK_VOLATILITY_PERCENT",
        gt=0,
        le=100,
    )
    risk_approval_ttl_seconds: int = Field(
        default=60, alias="RISK_APPROVAL_TTL_SECONDS", ge=1, le=3_600
    )
    max_entry_deviation_bps: float = Field(
        default=100.0, alias="MAX_ENTRY_DEVIATION_BPS", ge=0, le=10_000
    )

    # Paper trading account (simulated balance only — no real money, docs/18).
    paper_initial_balance: float = Field(default=10_000.0, alias="PAPER_INITIAL_BALANCE")
    fee_rate_percent: float = Field(
        default=0.08,
        alias="FEE_RATE_PERCENT",
        ge=0,
        le=10,
    )
    slippage_rate_percent: float = Field(
        default=0.02,
        alias="SLIPPAGE_RATE_PERCENT",
        ge=0,
        le=10,
    )
    backtest_half_spread_bps: float = Field(
        default=1.0,
        alias="BACKTEST_HALF_SPREAD_BPS",
        ge=0,
        le=1_000,
    )
    backtest_volume_impact_bps: float = Field(
        default=10.0,
        alias="BACKTEST_VOLUME_IMPACT_BPS",
        ge=0,
        le=10_000,
    )
    backtest_funding_rate_bps_per_8h: float = Field(
        default=0.0,
        alias="BACKTEST_FUNDING_RATE_BPS_PER_8H",
        ge=-1_000,
        le=1_000,
    )

    # Decision engine (docs/25-decision-engine.md).
    minimum_candidate_confidence: int = Field(default=70, alias="MINIMUM_CANDIDATE_CONFIDENCE")
    portfolio_max_target_weight_percent: float = Field(
        default=25.0,
        alias="PORTFOLIO_MAX_TARGET_WEIGHT_PERCENT",
        gt=0,
        le=100,
    )

    # Data quality (docs/32-data-quality.md).
    max_market_data_delay_ms: int = Field(default=5000, alias="MAX_MARKET_DATA_DELAY_MS")
    min_data_quality_score: int = Field(default=60, alias="MIN_DATA_QUALITY_SCORE")
    require_trusted_market_clock: bool = Field(
        default=True,
        alias="REQUIRE_TRUSTED_MARKET_CLOCK",
    )
    clock_probe_interval_seconds: float = Field(
        default=30.0,
        alias="CLOCK_PROBE_INTERVAL_SECONDS",
        ge=5.0,
        le=3600.0,
    )
    clock_observation_max_age_seconds: float = Field(
        default=90.0,
        alias="CLOCK_OBSERVATION_MAX_AGE_SECONDS",
        ge=5.0,
        le=7200.0,
    )
    clock_warning_offset_ms: float = Field(
        default=500.0,
        alias="CLOCK_WARNING_OFFSET_MS",
        ge=0,
    )
    clock_unsafe_offset_ms: float = Field(
        default=2000.0,
        alias="CLOCK_UNSAFE_OFFSET_MS",
        ge=0,
    )
    clock_warning_round_trip_ms: float = Field(
        default=1000.0,
        alias="CLOCK_WARNING_ROUND_TRIP_MS",
        ge=0,
    )
    clock_unsafe_round_trip_ms: float = Field(
        default=5000.0,
        alias="CLOCK_UNSAFE_ROUND_TRIP_MS",
        ge=0,
    )
    historical_backfill_max_candles: int = Field(
        default=100_000,
        alias="HISTORICAL_BACKFILL_MAX_CANDLES",
        ge=1,
        le=1_000_000,
    )
    backfill_worker_enabled: bool = Field(
        default=True,
        alias="BACKFILL_WORKER_ENABLED",
    )
    backfill_worker_poll_interval_seconds: float = Field(
        default=1.0,
        alias="BACKFILL_WORKER_POLL_INTERVAL_SECONDS",
        gt=0,
        le=60.0,
    )
    backfill_lease_seconds: int = Field(
        default=3_600,
        alias="BACKFILL_LEASE_SECONDS",
        ge=1,
        le=86_400,
    )
    backfill_max_attempts: int = Field(
        default=5,
        alias="BACKFILL_MAX_ATTEMPTS",
        ge=1,
        le=100,
    )
    backfill_retry_base_seconds: float = Field(
        default=5.0,
        alias="BACKFILL_RETRY_BASE_SECONDS",
        ge=0,
        le=3_600,
    )
    backfill_retry_max_seconds: float = Field(
        default=300.0,
        alias="BACKFILL_RETRY_MAX_SECONDS",
        ge=0,
        le=86_400,
    )
    data_lake_root: str = Field(
        default=".capital-cipher-data-lake",
        alias="DATA_LAKE_ROOT",
        min_length=1,
        max_length=1_024,
    )
    public_market_http_timeout_seconds: float = Field(
        default=10.0,
        alias="PUBLIC_MARKET_HTTP_TIMEOUT_SECONDS",
        ge=1.0,
        le=60.0,
    )
    binance_public_rest_url: str = Field(
        default="https://data-api.binance.vision",
        alias="BINANCE_PUBLIC_REST_URL",
    )
    bybit_public_rest_url: str = Field(
        default="https://api.bybit.com",
        alias="BYBIT_PUBLIC_REST_URL",
    )

    # Agent execution (docs/28-agent-lifecycle.md).
    agent_timeout_ms: int = Field(
        default=5000,
        alias="AGENT_TIMEOUT_MS",
        ge=1,
        le=300_000,
    )
    agent_max_attempts: int = Field(
        default=3,
        alias="AGENT_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    agent_max_concurrency: int = Field(
        default=8,
        alias="AGENT_MAX_CONCURRENCY",
        ge=1,
        le=100,
    )
    agent_worker_enabled: bool = Field(
        default=True,
        alias="AGENT_WORKER_ENABLED",
    )
    agent_worker_poll_interval_seconds: float = Field(
        default=0.25,
        alias="AGENT_WORKER_POLL_INTERVAL_SECONDS",
        gt=0,
        le=60,
    )
    agent_lease_seconds: int = Field(
        default=30,
        alias="AGENT_LEASE_SECONDS",
        ge=1,
        le=3_600,
    )
    agent_retry_base_seconds: float = Field(
        default=0.05,
        alias="AGENT_RETRY_BASE_SECONDS",
        ge=0,
        le=3_600,
    )
    agent_retry_max_seconds: float = Field(
        default=0.2,
        alias="AGENT_RETRY_MAX_SECONDS",
        ge=0,
        le=86_400,
    )

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

    @field_validator("app_env")
    @classmethod
    def normalize_app_environment(cls, value: str) -> str:
        environment = value.strip().lower()
        if environment not in {"local", "dev", "test", "ci", "staging"}:
            raise ValueError("APP_ENV must be local, dev, test, ci or staging")
        return environment

    @field_validator("oms_execution_environment")
    @classmethod
    def validate_oms_environment(cls, value: str) -> str:
        environment = value.strip().upper()
        if environment not in {"PAPER", "TESTNET"}:
            raise ValueError("OMS execution is restricted to PAPER or TESTNET")
        return environment

    @field_validator("oms_testnet_exchange")
    @classmethod
    def validate_oms_exchange(cls, value: str) -> str:
        exchange = value.strip().upper()
        if exchange not in {"BINANCE", "BYBIT"}:
            raise ValueError("OMS TESTNET exchange must be BINANCE or BYBIT")
        return exchange

    @field_validator("bybit_testnet_category")
    @classmethod
    def validate_bybit_testnet_category(cls, value: str) -> str:
        category = value.strip().lower()
        if category != "linear":
            raise ValueError(
                "Month 7 supports only Bybit linear TESTNET"
            )
        return category

    @field_validator("binance_testnet_rest_url")
    @classmethod
    def validate_binance_testnet_url(cls, value: str) -> str:
        if value.rstrip("/") != "https://testnet.binance.vision":
            raise ValueError(
                "Binance execution URL must be the exact Spot TESTNET"
            )
        return value.rstrip("/")

    @field_validator("bybit_testnet_rest_url")
    @classmethod
    def validate_bybit_testnet_url(cls, value: str) -> str:
        if value.rstrip("/") != "https://api-testnet.bybit.com":
            raise ValueError("Bybit execution URL must be the exact TESTNET")
        return value.rstrip("/")

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

    @field_validator("binance_public_rest_url", "bybit_public_rest_url")
    @classmethod
    def validate_public_market_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Public market REST URLs must be credential-free HTTPS origins"
            )
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_event_broker_configuration(self) -> "Settings":
        if self.event_broker_required and not self.redis_url:
            raise ValueError("REDIS_URL is required when EVENT_BROKER_REQUIRED is enabled")
        if self.clock_warning_offset_ms > self.clock_unsafe_offset_ms:
            raise ValueError("Clock offset thresholds are inconsistent")
        if (
            self.clock_warning_round_trip_ms
            > self.clock_unsafe_round_trip_ms
        ):
            raise ValueError("Clock round-trip thresholds are inconsistent")
        if self.backfill_retry_base_seconds > self.backfill_retry_max_seconds:
            raise ValueError("Backfill retry delay settings are inconsistent")
        if self.agent_retry_base_seconds > self.agent_retry_max_seconds:
            raise ValueError("Agent retry delay settings are inconsistent")
        if self.default_leverage > self.max_leverage_simulated:
            raise ValueError(
                "DEFAULT_LEVERAGE cannot exceed MAX_LEVERAGE_SIMULATED"
            )
        if self.var_min_observations > self.var_lookback:
            raise ValueError(
                "VAR_MIN_OBSERVATIONS cannot exceed VAR_LOOKBACK"
            )
        if self.oms_execution_environment == "TESTNET":
            if not self.oms_testnet_enabled:
                raise ValueError(
                    "OMS_TESTNET_ENABLED must be explicit for TESTNET"
                )
            if (
                self.oms_testnet_acknowledgement
                != "TESTNET_ONLY_NO_REAL_FUNDS"
            ):
                raise ValueError(
                    "TESTNET requires OMS_TESTNET_ACKNOWLEDGEMENT="
                    "TESTNET_ONLY_NO_REAL_FUNDS"
                )
        if self.app_env == "staging":
            violations: list[str] = []
            database = urlsplit(self.database_url)
            broker = urlsplit(self.redis_url or "")
            if self.system_mode != "PAPER":
                violations.append("SYSTEM_MODE must be PAPER")
            if self.oms_execution_environment != "PAPER":
                violations.append("OMS_EXECUTION_ENVIRONMENT must be PAPER")
            if self.oms_testnet_enabled:
                violations.append("OMS_TESTNET_ENABLED must be disabled")
            if self.oms_testnet_acknowledgement:
                violations.append("OMS_TESTNET_ACKNOWLEDGEMENT must be empty")
            if self.oms_worker_enabled:
                violations.append("OMS_WORKER_ENABLED must be disabled")
            if self.oms_reconciliation_enabled:
                violations.append("OMS_RECONCILIATION_ENABLED must be disabled")
            if database.scheme != "postgresql+asyncpg" or not database.hostname:
                violations.append("DATABASE_URL must use postgresql+asyncpg")
            if self.database_pool_size + self.database_max_overflow > 10:
                violations.append(
                    "staging database connections must be bounded to 10"
                )
            if not self.event_broker_required:
                violations.append("EVENT_BROKER_REQUIRED must be enabled")
            if broker.scheme not in {"redis", "rediss"} or not broker.hostname:
                violations.append("REDIS_URL must use redis or rediss")
            if not self.enable_market_data:
                violations.append("ENABLE_MARKET_DATA must be enabled")
            if not self.operations_monitor_enabled:
                violations.append("OPERATIONS_MONITOR_ENABLED must be enabled")
            if not self.agent_worker_enabled:
                violations.append("AGENT_WORKER_ENABLED must be enabled")
            if not self.backfill_worker_enabled:
                violations.append("BACKFILL_WORKER_ENABLED must be enabled")
            if self.admin_api_key is None:
                violations.append("ADMIN_API_KEY is required")
            if self.default_leverage != 1 or self.max_leverage_simulated != 1:
                violations.append("staging leverage must be fixed at 1x")
            if not self.cors_allowed_origins_list or any(
                origin == "*" for origin in self.cors_allowed_origins_list
            ):
                violations.append("CORS_ALLOWED_ORIGINS must be explicit")
            if violations:
                raise ValueError(
                    "Staging PAPER invariant violation: " + "; ".join(violations)
                )
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

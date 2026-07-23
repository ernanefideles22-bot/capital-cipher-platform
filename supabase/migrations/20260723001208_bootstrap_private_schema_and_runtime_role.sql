-- Complete the migration-owned private schema and establish a least-privilege
-- NOLOGIN group role for the hosted backend. A separate environment-specific
-- LOGIN role must be created outside Git and granted membership in this role.

set lock_timeout = '5s';
set statement_timeout = '60s';

create schema if not exists capital_cipher;
revoke all on schema capital_cipher from public;

CREATE TABLE capital_cipher.agent_outputs (
	id VARCHAR(36) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	agent_name TEXT NOT NULL,
	status TEXT NOT NULL,
	signal TEXT,
	confidence INTEGER,
	reason TEXT,
	evidence JSONB,
	warnings JSONB,
	latency_ms INTEGER,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_agent_outputs_agent_name ON capital_cipher.agent_outputs (agent_name);
CREATE INDEX ix_capital_cipher_agent_outputs_correlation_id ON capital_cipher.agent_outputs (correlation_id);
CREATE INDEX ix_capital_cipher_agent_outputs_created_at ON capital_cipher.agent_outputs (created_at);

CREATE TABLE capital_cipher.audit_logs (
	id VARCHAR(36) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	audit_type TEXT NOT NULL,
	entity_type TEXT NOT NULL,
	entity_id VARCHAR(36),
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_audit_logs_audit_type ON capital_cipher.audit_logs (audit_type);
CREATE INDEX ix_capital_cipher_audit_logs_correlation_id ON capital_cipher.audit_logs (correlation_id);

CREATE TABLE capital_cipher.candle_observations (
	candle_id VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	open NUMERIC(38, 18) NOT NULL,
	high NUMERIC(38, 18) NOT NULL,
	low NUMERIC(38, 18) NOT NULL,
	close NUMERIC(38, 18) NOT NULL,
	volume NUMERIC(38, 18) NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ingest_lag_ms INTEGER NOT NULL,
	quality_score INTEGER,
	quality_status VARCHAR(16) NOT NULL,
	quality_warnings JSONB NOT NULL,
	quality_errors JSONB NOT NULL,
	recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (candle_id),
	CONSTRAINT uq_candle_observations_series_time UNIQUE (exchange, symbol, timeframe, closed_at),
	CONSTRAINT ck_candle_observations_prices_positive CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
	CONSTRAINT ck_candle_observations_high CHECK (high >= open AND high >= close AND high >= low),
	CONSTRAINT ck_candle_observations_low CHECK (low <= open AND low <= close AND low <= high),
	CONSTRAINT ck_candle_observations_volume CHECK (volume >= 0),
	CONSTRAINT ck_candle_observations_quality_score CHECK (quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100))
);
CREATE INDEX ix_candle_observations_quality_received ON capital_cipher.candle_observations (quality_status, received_at);
CREATE INDEX ix_candle_observations_series_time ON capital_cipher.candle_observations (exchange, symbol, timeframe, closed_at);

CREATE TABLE capital_cipher.clock_observations (
	observation_id VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	source TEXT NOT NULL,
	request_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source_at TIMESTAMP WITH TIME ZONE NOT NULL,
	response_received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	offset_ms NUMERIC(20, 6) NOT NULL,
	round_trip_ms NUMERIC(20, 6) NOT NULL,
	status VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (observation_id),
	CONSTRAINT ck_clock_observations_round_trip CHECK (round_trip_ms >= 0),
	CONSTRAINT ck_clock_observations_status CHECK (status IN ('SYNCED', 'WARNING', 'UNSAFE'))
);
CREATE INDEX ix_clock_observations_source_time ON capital_cipher.clock_observations (source, response_received_at);

CREATE TABLE capital_cipher.dataset_manifests (
	dataset_hash VARCHAR(64) NOT NULL,
	dataset_id VARCHAR(80) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	candle_contract_version VARCHAR(16) NOT NULL,
	dataset_type VARCHAR(32) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	row_count INTEGER NOT NULL,
	selection JSONB NOT NULL,
	quality_summary JSONB NOT NULL,
	clock_status VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (dataset_hash),
	CONSTRAINT ck_dataset_manifests_row_count CHECK (row_count > 0),
	CONSTRAINT ck_dataset_manifests_time_range CHECK (start_at <= end_at),
	UNIQUE (dataset_id)
);
CREATE INDEX ix_dataset_manifests_series_range ON capital_cipher.dataset_manifests (exchange, symbol, timeframe, start_at, end_at);

CREATE TABLE capital_cipher.decisions (
	id VARCHAR(36) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	candidate_action TEXT NOT NULL,
	confidence INTEGER NOT NULL,
	reason TEXT,
	agent_summary JSONB NOT NULL,
	risk_status TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_decisions_correlation_id ON capital_cipher.decisions (correlation_id);

CREATE TABLE capital_cipher.event_journal (
	message_id VARCHAR(36) NOT NULL,
	event_id VARCHAR(36) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	topic TEXT NOT NULL,
	event_type TEXT NOT NULL,
	source TEXT NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (message_id)
);
CREATE INDEX ix_capital_cipher_event_journal_correlation_id ON capital_cipher.event_journal (correlation_id);
CREATE INDEX ix_capital_cipher_event_journal_created_at ON capital_cipher.event_journal (created_at);
CREATE UNIQUE INDEX ix_capital_cipher_event_journal_event_id ON capital_cipher.event_journal (event_id);
CREATE INDEX ix_capital_cipher_event_journal_event_type ON capital_cipher.event_journal (event_type);
CREATE INDEX ix_capital_cipher_event_journal_topic ON capital_cipher.event_journal (topic);

CREATE TABLE capital_cipher.historical_backfill_jobs (
	job_id VARCHAR(64) NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	source TEXT NOT NULL,
	status VARCHAR(16) NOT NULL,
	retrieved_count INTEGER NOT NULL,
	inserted_count INTEGER NOT NULL,
	remaining_gap_count INTEGER NOT NULL,
	attempt_count INTEGER NOT NULL,
	dataset_hash VARCHAR(64),
	clock_observation_id VARCHAR(64),
	clock_status VARCHAR(16) NOT NULL,
	error_code VARCHAR(64),
	error_message VARCHAR(500),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (job_id),
	CONSTRAINT ck_historical_backfill_jobs_time_range CHECK (start_at <= end_at),
	CONSTRAINT ck_historical_backfill_jobs_counts CHECK (retrieved_count >= 0 AND inserted_count >= 0 AND remaining_gap_count >= 0 AND attempt_count >= 0),
	CONSTRAINT ck_historical_backfill_jobs_status CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'BLOCKED', 'FAILED')),
	CONSTRAINT ck_historical_backfill_jobs_clock_status CHECK (clock_status IN ('SYNCED', 'WARNING', 'UNSAFE', 'UNKNOWN')),
	UNIQUE (request_fingerprint)
);
CREATE INDEX ix_historical_backfill_jobs_series_range ON capital_cipher.historical_backfill_jobs (exchange, symbol, timeframe, start_at, end_at);
CREATE INDEX ix_historical_backfill_jobs_status_updated ON capital_cipher.historical_backfill_jobs (status, updated_at);

CREATE TABLE capital_cipher.market_candles (
	id VARCHAR(36) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	open NUMERIC NOT NULL,
	high NUMERIC NOT NULL,
	low NUMERIC NOT NULL,
	close NUMERIC NOT NULL,
	volume NUMERIC NOT NULL,
	closed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_market_candles_closed_at ON capital_cipher.market_candles (closed_at);
CREATE INDEX ix_capital_cipher_market_candles_symbol ON capital_cipher.market_candles (symbol);

CREATE TABLE capital_cipher.market_data_gaps (
	gap_id VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	missing_count INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	backfill_job_id VARCHAR(64),
	PRIMARY KEY (gap_id),
	CONSTRAINT ck_market_data_gaps_missing_count CHECK (missing_count > 0),
	CONSTRAINT ck_market_data_gaps_time_range CHECK (start_at <= end_at),
	CONSTRAINT ck_market_data_gaps_status CHECK (status IN ('OPEN', 'FILLING', 'RESOLVED', 'FAILED'))
);
CREATE INDEX ix_market_data_gaps_series_status_range ON capital_cipher.market_data_gaps (exchange, symbol, timeframe, status, start_at);
CREATE INDEX ix_market_data_gaps_status_detected ON capital_cipher.market_data_gaps (status, detected_at);

CREATE TABLE capital_cipher.paper_orders (
	id VARCHAR(36) NOT NULL,
	decision_id VARCHAR(36) NOT NULL,
	risk_check_id VARCHAR(36) NOT NULL,
	approval_id VARCHAR(64),
	request_fingerprint VARCHAR(64),
	correlation_id VARCHAR(36) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT,
	strategy TEXT NOT NULL,
	side TEXT NOT NULL,
	entry_price NUMERIC NOT NULL,
	stop_loss NUMERIC,
	take_profit NUMERIC,
	position_size NUMERIC NOT NULL,
	leverage NUMERIC NOT NULL,
	status TEXT NOT NULL,
	fees_estimated NUMERIC,
	slippage_estimated NUMERIC,
	opened_at TIMESTAMP WITH TIME ZONE,
	closed_at TIMESTAMP WITH TIME ZONE,
	pnl NUMERIC,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (approval_id)
);
CREATE INDEX ix_capital_cipher_paper_orders_correlation_id ON capital_cipher.paper_orders (correlation_id);

CREATE TABLE capital_cipher.raw_data_objects (
	object_hash VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	object_uri TEXT NOT NULL,
	content_type VARCHAR(64) NOT NULL,
	content_encoding VARCHAR(32) NOT NULL,
	uncompressed_bytes INTEGER NOT NULL,
	stored_bytes INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (object_hash),
	CONSTRAINT ck_raw_data_objects_sizes CHECK (uncompressed_bytes > 0 AND stored_bytes > 0),
	CONSTRAINT uq_raw_data_objects_uri UNIQUE (object_uri)
);

CREATE TABLE capital_cipher.raw_market_events (
	event_id VARCHAR(36) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	source TEXT NOT NULL,
	exchange TEXT NOT NULL,
	event_type TEXT NOT NULL,
	symbol TEXT,
	occurred_at TIMESTAMP WITH TIME ZONE,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSONB NOT NULL,
	payload_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (event_id)
);
CREATE INDEX ix_capital_cipher_raw_market_events_event_type ON capital_cipher.raw_market_events (event_type);
CREATE INDEX ix_capital_cipher_raw_market_events_exchange ON capital_cipher.raw_market_events (exchange);
CREATE INDEX ix_capital_cipher_raw_market_events_occurred_at ON capital_cipher.raw_market_events (occurred_at);
CREATE INDEX ix_capital_cipher_raw_market_events_payload_sha256 ON capital_cipher.raw_market_events (payload_sha256);
CREATE INDEX ix_capital_cipher_raw_market_events_received_at ON capital_cipher.raw_market_events (received_at);
CREATE INDEX ix_capital_cipher_raw_market_events_source ON capital_cipher.raw_market_events (source);
CREATE INDEX ix_capital_cipher_raw_market_events_symbol ON capital_cipher.raw_market_events (symbol);

CREATE TABLE capital_cipher.replay_checkpoints (
	replay_id VARCHAR(128) NOT NULL,
	consumer_name VARCHAR(128) NOT NULL,
	topic VARCHAR(128) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	dataset_hash VARCHAR(64) NOT NULL,
	next_offset INTEGER NOT NULL,
	last_event_id VARCHAR(64),
	events_processed INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (replay_id, consumer_name, topic)
);
CREATE INDEX ix_replay_checkpoints_status_updated ON capital_cipher.replay_checkpoints (status, updated_at);

CREATE TABLE capital_cipher.risk_checks (
	id VARCHAR(36) NOT NULL,
	decision_id VARCHAR(36) NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	risk_status TEXT NOT NULL,
	approved BOOLEAN NOT NULL,
	position_size NUMERIC,
	risk_percent NUMERIC,
	stop_loss NUMERIC,
	take_profit NUMERIC,
	risk_reward NUMERIC,
	reason TEXT,
	warnings JSONB,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_risk_checks_correlation_id ON capital_cipher.risk_checks (correlation_id);
CREATE INDEX ix_capital_cipher_risk_checks_decision_id ON capital_cipher.risk_checks (decision_id);

CREATE TABLE capital_cipher.system_events (
	id VARCHAR(36) NOT NULL,
	event_type TEXT NOT NULL,
	source TEXT NOT NULL,
	correlation_id VARCHAR(36) NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_capital_cipher_system_events_correlation_id ON capital_cipher.system_events (correlation_id);
CREATE INDEX ix_capital_cipher_system_events_created_at ON capital_cipher.system_events (created_at);
CREATE INDEX ix_capital_cipher_system_events_event_type ON capital_cipher.system_events (event_type);

CREATE TABLE capital_cipher.backfill_queue_items (
	queue_id VARCHAR(64) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	exchange TEXT NOT NULL,
	symbol TEXT NOT NULL,
	timeframe TEXT NOT NULL,
	start_at TIMESTAMP WITH TIME ZONE NOT NULL,
	end_at TIMESTAMP WITH TIME ZONE NOT NULL,
	max_candles INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	attempt_count INTEGER NOT NULL,
	max_attempts INTEGER NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	leased_by VARCHAR(128),
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	last_error_code VARCHAR(64),
	last_error_message VARCHAR(500),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (queue_id),
	CONSTRAINT ck_backfill_queue_items_time_range CHECK (start_at <= end_at),
	CONSTRAINT ck_backfill_queue_items_max_candles CHECK (max_candles > 0 AND max_candles <= 1000000),
	CONSTRAINT ck_backfill_queue_items_attempts CHECK (attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0 AND max_attempts <= 100),
	CONSTRAINT ck_backfill_queue_items_status CHECK (status IN ('PENDING', 'LEASED', 'RETRY', 'COMPLETED', 'DEAD_LETTER')),
	CONSTRAINT ck_backfill_queue_items_lease CHECK ((status = 'LEASED' AND leased_by IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'LEASED' AND leased_by IS NULL AND lease_expires_at IS NULL)),
	CONSTRAINT ck_backfill_queue_items_terminal CHECK ((status IN ('COMPLETED', 'DEAD_LETTER') AND completed_at IS NOT NULL) OR (status NOT IN ('COMPLETED', 'DEAD_LETTER') AND completed_at IS NULL)),
	UNIQUE (job_id),
	FOREIGN KEY(job_id) REFERENCES capital_cipher.historical_backfill_jobs (job_id) ON DELETE CASCADE
);
CREATE INDEX ix_backfill_queue_expired_leases ON capital_cipher.backfill_queue_items (lease_expires_at, created_at) WHERE status = 'LEASED';
CREATE INDEX ix_backfill_queue_ready ON capital_cipher.backfill_queue_items (status, available_at, created_at) WHERE status IN ('PENDING', 'RETRY');

CREATE TABLE capital_cipher.backfill_raw_pages (
	page_id VARCHAR(64) NOT NULL,
	schema_version VARCHAR(16) NOT NULL,
	job_id VARCHAR(64) NOT NULL,
	attempt_count INTEGER NOT NULL,
	page_index INTEGER NOT NULL,
	object_hash VARCHAR(64) NOT NULL,
	source TEXT NOT NULL,
	endpoint TEXT NOT NULL,
	request_params JSONB NOT NULL,
	fetched_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (page_id),
	CONSTRAINT ck_backfill_raw_pages_position CHECK (attempt_count > 0 AND page_index >= 0),
	CONSTRAINT uq_backfill_raw_pages_attempt_page UNIQUE (job_id, attempt_count, page_index),
	FOREIGN KEY(job_id) REFERENCES capital_cipher.historical_backfill_jobs (job_id) ON DELETE CASCADE,
	FOREIGN KEY(object_hash) REFERENCES capital_cipher.raw_data_objects (object_hash) ON DELETE RESTRICT
);
CREATE INDEX ix_backfill_raw_pages_job_attempt ON capital_cipher.backfill_raw_pages (job_id, attempt_count, page_index);
CREATE INDEX ix_backfill_raw_pages_object ON capital_cipher.backfill_raw_pages (object_hash);

CREATE TABLE capital_cipher.event_outbox (
	event_id VARCHAR(36) NOT NULL,
	broker_message_id TEXT,
	published_at TIMESTAMP WITH TIME ZONE,
	publish_attempts INTEGER NOT NULL,
	last_error_type TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (event_id),
	FOREIGN KEY(event_id) REFERENCES capital_cipher.event_journal (event_id) ON DELETE CASCADE
);
CREATE INDEX ix_capital_cipher_event_outbox_published_at ON capital_cipher.event_outbox (published_at);
CREATE INDEX ix_event_outbox_pending_created ON capital_cipher.event_outbox (created_at) WHERE published_at IS NULL;

do $block$
begin
    if not exists (
        select 1 from pg_roles where rolname = 'capital_cipher_runtime'
    ) then
        execute
            'create role capital_cipher_runtime '
            'nologin nosuperuser nocreatedb nocreaterole '
            'noreplication nobypassrls';
    end if;

    -- Hosted Supabase runs migrations as an administrative role that can
    -- create ordinary roles but cannot ALTER the SUPERUSER or BYPASSRLS
    -- attributes. Fail closed if a pre-existing role is not least privilege
    -- instead of attempting a privileged role alteration.
    if exists (
        select 1
        from pg_roles
        where rolname = 'capital_cipher_runtime'
          and (
              rolsuper
              or rolcreatedb
              or rolcreaterole
              or rolcanlogin
              or rolreplication
              or rolbypassrls
          )
    ) then
        raise exception
            'capital_cipher_runtime must be a NOLOGIN least-privilege role'
            using errcode = '42501';
    end if;
end;
$block$;

revoke all on schema capital_cipher from public;
revoke all on all tables in schema capital_cipher from public;
revoke all on all sequences in schema capital_cipher from public;
revoke all on all functions in schema capital_cipher from public;

do $block$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on schema capital_cipher from anon;
        revoke all on all tables in schema capital_cipher from anon;
        revoke all on all sequences in schema capital_cipher from anon;
        revoke all on all functions in schema capital_cipher from anon;
    end if;
    if exists (
        select 1 from pg_roles where rolname = 'authenticated'
    ) then
        revoke all on schema capital_cipher from authenticated;
        revoke all on all tables in schema capital_cipher
            from authenticated;
        revoke all on all sequences in schema capital_cipher
            from authenticated;
        revoke all on all functions in schema capital_cipher
            from authenticated;
    end if;
end;
$block$;

grant usage on schema capital_cipher to capital_cipher_runtime;
grant select, insert on all tables in schema capital_cipher
    to capital_cipher_runtime;
grant usage, select on all sequences in schema capital_cipher
    to capital_cipher_runtime;

grant update on
    capital_cipher.event_outbox,
    capital_cipher.decisions,
    capital_cipher.market_data_gaps,
    capital_cipher.historical_backfill_jobs,
    capital_cipher.backfill_queue_items,
    capital_cipher.replay_checkpoints,
    capital_cipher.agent_execution_jobs,
    capital_cipher.order_approvals,
    capital_cipher.risk_control_state,
    capital_cipher.paper_orders,
    capital_cipher.oms_orders,
    capital_cipher.execution_commands,
    capital_cipher.reconciliation_runs
to capital_cipher_runtime;

revoke delete, truncate, references, trigger
on all tables in schema capital_cipher
from capital_cipher_runtime;
revoke all on all functions in schema capital_cipher
from capital_cipher_runtime;

do $block$
declare
    target_table text;
begin
    for target_table in
        select tablename
        from pg_tables
        where schemaname = 'capital_cipher'
        order by tablename
    loop
        execute format(
            'alter table %I.%I enable row level security',
            'capital_cipher',
            target_table
        );
        execute format(
            'drop policy if exists runtime_select on %I.%I',
            'capital_cipher',
            target_table
        );
        execute format(
            'create policy runtime_select on %I.%I '
            'for select to capital_cipher_runtime using (true)',
            'capital_cipher',
            target_table
        );
        execute format(
            'drop policy if exists runtime_insert on %I.%I',
            'capital_cipher',
            target_table
        );
        execute format(
            'create policy runtime_insert on %I.%I '
            'for insert to capital_cipher_runtime with check (true)',
            'capital_cipher',
            target_table
        );
    end loop;
end;
$block$;

do $block$
declare
    target_table text;
begin
    foreach target_table in array array[
        'event_outbox',
        'decisions',
        'market_data_gaps',
        'historical_backfill_jobs',
        'backfill_queue_items',
        'replay_checkpoints',
        'agent_execution_jobs',
        'order_approvals',
        'risk_control_state',
        'paper_orders',
        'oms_orders',
        'execution_commands',
        'reconciliation_runs'
    ]
    loop
        execute format(
            'drop policy if exists runtime_update on %I.%I',
            'capital_cipher',
            target_table
        );
        execute format(
            'create policy runtime_update on %I.%I '
            'for update to capital_cipher_runtime '
            'using (true) with check (true)',
            'capital_cipher',
            target_table
        );
    end loop;
end;
$block$;

comment on role capital_cipher_runtime is
    'NOLOGIN least-privilege group role for Capital Cipher hosted runtime.';

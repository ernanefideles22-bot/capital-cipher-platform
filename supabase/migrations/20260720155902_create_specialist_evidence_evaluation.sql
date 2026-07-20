set lock_timeout = '5s';
set statement_timeout = '60s';

create table capital_cipher.specialist_evidence (
    evidence_id varchar(64) primary key,
    schema_version varchar(16) not null,
    domain varchar(16) not null,
    metric_name varchar(64) not null,
    scope varchar(32) not null,
    source varchar(128) not null,
    source_event_id varchar(256) not null,
    value numeric(38, 18) not null,
    unit varchar(32) not null,
    quality_score integer not null,
    observed_at timestamptz not null,
    received_at timestamptz not null,
    provenance_uri text,
    payload_sha256 varchar(64) not null,
    constraint uq_specialist_evidence_source_event
        unique (source, source_event_id),
    constraint ck_specialist_evidence_id
        check (evidence_id ~ '^[a-f0-9]{64}$'),
    constraint ck_specialist_evidence_payload
        check (payload_sha256 ~ '^[a-f0-9]{64}$'),
    constraint ck_specialist_evidence_domain
        check (domain in ('DERIVATIVES', 'MACRO', 'ONCHAIN', 'NEWS')),
    constraint ck_specialist_evidence_metric
        check (metric_name ~ '^[a-z][a-z0-9_]{1,63}$'),
    constraint ck_specialist_evidence_scope
        check (scope = 'GLOBAL' or scope ~ '^[A-Z0-9._-]{2,32}$'),
    constraint ck_specialist_evidence_quality
        check (quality_score between 0 and 100),
    constraint ck_specialist_evidence_time
        check (received_at >= observed_at)
);

create index ix_specialist_evidence_lookup
    on capital_cipher.specialist_evidence (
        domain,
        metric_name,
        scope,
        observed_at desc
    );

create table capital_cipher.agent_forecasts (
    forecast_id varchar(64) primary key,
    schema_version varchar(16) not null,
    correlation_id varchar(36) not null,
    agent_name varchar(128) not null,
    agent_version varchar(32) not null,
    definition_hash varchar(64) not null,
    symbol varchar(32) not null,
    timeframe varchar(16) not null,
    signal varchar(16) not null,
    confidence integer not null,
    probability_up numeric(20, 18) not null,
    reference_price numeric(38, 18) not null,
    forecast_at timestamptz not null,
    target_at timestamptz not null,
    horizon_seconds integer not null,
    decision_role varchar(16) not null,
    created_at timestamptz not null,
    constraint ck_agent_forecast_id
        check (forecast_id ~ '^[a-f0-9]{64}$'),
    constraint ck_agent_forecast_definition
        check (definition_hash ~ '^[a-f0-9]{64}$'),
    constraint ck_agent_forecast_signal
        check (
            signal in ('BUY', 'SELL', 'HOLD', 'WAIT', 'BLOCK', 'NEUTRAL')
        ),
    constraint ck_agent_forecast_confidence
        check (confidence between 0 and 100),
    constraint ck_agent_forecast_probability
        check (probability_up between 0 and 1),
    constraint ck_agent_forecast_role
        check (decision_role in ('PRIMARY', 'SHADOW')),
    constraint ck_agent_forecast_horizon
        check (
            reference_price > 0
            and horizon_seconds > 0
            and target_at = forecast_at
                + make_interval(secs => horizon_seconds)
        )
);

create index ix_agent_forecasts_pending
    on capital_cipher.agent_forecasts (symbol, timeframe, target_at);
create index ix_agent_forecasts_agent
    on capital_cipher.agent_forecasts (
        agent_name,
        agent_version,
        forecast_at desc
    );

create table capital_cipher.agent_forecast_outcomes (
    outcome_id varchar(64) primary key,
    schema_version varchar(16) not null,
    forecast_id varchar(64) not null unique
        references capital_cipher.agent_forecasts (forecast_id)
        on delete restrict,
    realized_at timestamptz not null,
    realized_price numeric(38, 18) not null,
    realized_return numeric(38, 18) not null,
    realized_up numeric(20, 18) not null,
    correct boolean,
    brier_loss numeric(20, 18) not null,
    ensemble_probability_up numeric(20, 18) not null,
    ensemble_brier_loss numeric(20, 18) not null,
    leave_one_out_probability_up numeric(20, 18) not null,
    leave_one_out_brier_loss numeric(20, 18) not null,
    marginal_contribution numeric(20, 18) not null,
    cohort_size integer not null,
    created_at timestamptz not null,
    constraint ck_agent_forecast_outcome_id
        check (outcome_id ~ '^[a-f0-9]{64}$'),
    constraint ck_agent_forecast_outcome_price
        check (realized_price > 0),
    constraint ck_agent_forecast_outcome_realized
        check (realized_up between 0 and 1),
    constraint ck_agent_forecast_outcome_probabilities
        check (
            ensemble_probability_up between 0 and 1
            and leave_one_out_probability_up between 0 and 1
        ),
    constraint ck_agent_forecast_outcome_losses
        check (
            brier_loss between 0 and 1
            and ensemble_brier_loss between 0 and 1
            and leave_one_out_brier_loss between 0 and 1
        ),
    constraint ck_agent_forecast_outcome_contribution
        check (marginal_contribution between -1 and 1),
    constraint ck_agent_forecast_outcome_cohort
        check (cohort_size >= 1)
);

create index ix_agent_forecast_outcomes_realized
    on capital_cipher.agent_forecast_outcomes (realized_at desc);

create or replace function
    capital_cipher.reject_specialist_evaluation_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'specialist evaluation evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_specialist_evaluation_mutation()
from public;

create trigger trg_specialist_evidence_immutable
before update or delete on capital_cipher.specialist_evidence
for each row execute function
    capital_cipher.reject_specialist_evaluation_mutation();
create trigger trg_agent_forecasts_immutable
before update or delete on capital_cipher.agent_forecasts
for each row execute function
    capital_cipher.reject_specialist_evaluation_mutation();
create trigger trg_agent_forecast_outcomes_immutable
before update or delete on capital_cipher.agent_forecast_outcomes
for each row execute function
    capital_cipher.reject_specialist_evaluation_mutation();

alter table capital_cipher.specialist_evidence enable row level security;
alter table capital_cipher.agent_forecasts enable row level security;
alter table capital_cipher.agent_forecast_outcomes enable row level security;

revoke all on all tables in schema capital_cipher from public;
revoke all on all sequences in schema capital_cipher from public;
revoke all on all functions in schema capital_cipher from public;

do $block$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on all tables in schema capital_cipher from anon;
        revoke all on all sequences in schema capital_cipher from anon;
        revoke all on all functions in schema capital_cipher from anon;
    end if;
    if exists (
        select 1 from pg_roles where rolname = 'authenticated'
    ) then
        revoke all on all tables in schema capital_cipher
            from authenticated;
        revoke all on all sequences in schema capital_cipher
            from authenticated;
        revoke all on all functions in schema capital_cipher
            from authenticated;
    end if;
end;
$block$;

-- Month 10 operational observability, SLO, cost and resilience evidence.
-- Private direct-Postgres schema; no browser/Data API access is granted.

create table capital_cipher.operational_metric_snapshots (
    snapshot_id varchar(36) primary key,
    schema_version varchar(16) not null,
    correlation_id varchar(64) not null,
    registered_agents integer not null,
    active_agents integer not null,
    payload jsonb not null,
    captured_at timestamptz not null,
    constraint ck_operational_metric_snapshot_agents
        check (
            registered_agents >= 0
            and active_agents >= 0
            and active_agents <= registered_agents
        )
);

create index ix_operational_metric_snapshots_captured
    on capital_cipher.operational_metric_snapshots (
        captured_at desc,
        snapshot_id
    );

create table capital_cipher.slo_evaluations (
    evaluation_id varchar(36) primary key,
    schema_version varchar(16) not null,
    slo_name varchar(128) not null,
    status varchar(16) not null,
    sample_count integer not null,
    payload jsonb not null,
    evaluated_at timestamptz not null,
    constraint ck_slo_evaluation_status
        check (
            status in ('NO_DATA', 'HEALTHY', 'WARNING', 'BREACHED')
        ),
    constraint ck_slo_evaluation_samples
        check (sample_count >= 0)
);

create index ix_slo_evaluations_name_evaluated
    on capital_cipher.slo_evaluations (
        slo_name,
        evaluated_at desc
    );

create table capital_cipher.operational_alert_events (
    alert_event_id varchar(36) primary key,
    schema_version varchar(16) not null,
    alert_key varchar(160) not null,
    lifecycle_sequence integer not null,
    event_type varchar(16) not null,
    severity varchar(16) not null,
    payload jsonb not null,
    occurred_at timestamptz not null,
    constraint ck_operational_alert_event_type
        check (event_type in ('OPENED', 'RESOLVED')),
    constraint ck_operational_alert_severity
        check (severity in ('WARNING', 'ERROR', 'CRITICAL')),
    constraint ck_operational_alert_sequence
        check (lifecycle_sequence >= 1),
    constraint uq_operational_alert_lifecycle
        unique (alert_key, lifecycle_sequence)
);

create index ix_operational_alert_events_key_occurred
    on capital_cipher.operational_alert_events (
        alert_key,
        occurred_at desc
    );

create table capital_cipher.cost_usage_records (
    usage_id varchar(36) primary key,
    schema_version varchar(16) not null,
    cost_center varchar(32) not null,
    resource varchar(128) not null,
    estimated_cost_usd numeric(20, 8) not null,
    payload jsonb not null,
    observed_at timestamptz not null,
    constraint ck_cost_usage_center
        check (
            cost_center in (
                'AGENT_RUNTIME',
                'EXTERNAL_DATA',
                'STORAGE',
                'OBSERVABILITY'
            )
        ),
    constraint ck_cost_usage_nonnegative
        check (estimated_cost_usd >= 0)
);

create index ix_cost_usage_records_center_observed
    on capital_cipher.cost_usage_records (
        cost_center,
        observed_at desc
    );

create table capital_cipher.resilience_test_runs (
    run_id varchar(36) primary key,
    schema_version varchar(16) not null,
    run_type varchar(16) not null,
    scenario varchar(160) not null,
    status varchar(16) not null,
    payload jsonb not null,
    completed_at timestamptz not null,
    constraint ck_resilience_test_run_type
        check (run_type in ('LOAD', 'CHAOS', 'RECOVERY')),
    constraint ck_resilience_test_run_status
        check (status in ('PASSED', 'FAILED'))
);

create index ix_resilience_test_runs_type_completed
    on capital_cipher.resilience_test_runs (
        run_type,
        completed_at desc
    );

create or replace function
    capital_cipher.reject_operational_evidence_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'operational evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_operational_evidence_mutation()
from public;

create trigger trg_operational_metric_snapshots_immutable
before update or delete
on capital_cipher.operational_metric_snapshots
for each row execute function
    capital_cipher.reject_operational_evidence_mutation();

create trigger trg_slo_evaluations_immutable
before update or delete
on capital_cipher.slo_evaluations
for each row execute function
    capital_cipher.reject_operational_evidence_mutation();

create trigger trg_operational_alert_events_immutable
before update or delete
on capital_cipher.operational_alert_events
for each row execute function
    capital_cipher.reject_operational_evidence_mutation();

create trigger trg_cost_usage_records_immutable
before update or delete
on capital_cipher.cost_usage_records
for each row execute function
    capital_cipher.reject_operational_evidence_mutation();

create trigger trg_resilience_test_runs_immutable
before update or delete
on capital_cipher.resilience_test_runs
for each row execute function
    capital_cipher.reject_operational_evidence_mutation();

alter table capital_cipher.operational_metric_snapshots
    enable row level security;
alter table capital_cipher.slo_evaluations
    enable row level security;
alter table capital_cipher.operational_alert_events
    enable row level security;
alter table capital_cipher.cost_usage_records
    enable row level security;
alter table capital_cipher.resilience_test_runs
    enable row level security;

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

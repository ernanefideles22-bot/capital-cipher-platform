-- Month 11 prolonged PAPER shadow validation evidence.
-- Private direct-Postgres schema; no browser/Data API access is granted.

create table capital_cipher.shadow_campaign_checkpoints (
    checkpoint_id varchar(36) primary key,
    schema_version varchar(16) not null,
    campaign_id varchar(36) not null,
    sequence integer not null,
    status varchar(40) not null,
    acceptance_status varchar(16) not null,
    registered_agents integer not null,
    primary_agents integer not null,
    shadow_agents integer not null,
    executed_agents integer not null,
    payload jsonb not null,
    replay_at timestamptz not null,
    captured_at timestamptz not null,
    constraint ck_shadow_checkpoint_status
        check (
            status in (
                'EXECUTED',
                'SUSPENDED_SAFE_DEGRADATION',
                'BLOCKED_RECONCILIATION',
                'BLOCKED_RISK'
            )
        ),
    constraint ck_shadow_checkpoint_acceptance
        check (acceptance_status in ('PASSED', 'FAILED')),
    constraint ck_shadow_checkpoint_cohort
        check (
            registered_agents = 300
            and primary_agents = 3
            and shadow_agents = 297
        ),
    constraint ck_shadow_checkpoint_executions
        check (executed_agents >= 0 and executed_agents <= 300),
    constraint uq_shadow_checkpoint_sequence
        unique (campaign_id, sequence)
);

create index ix_shadow_campaign_checkpoints_campaign_replay
    on capital_cipher.shadow_campaign_checkpoints (
        campaign_id,
        replay_at desc
    );

create table capital_cipher.shadow_validation_reports (
    report_id varchar(36) primary key,
    schema_version varchar(16) not null,
    campaign_id varchar(36) not null,
    status varchar(16) not null,
    payload jsonb not null,
    started_at timestamptz not null,
    completed_at timestamptz not null,
    constraint ck_shadow_validation_report_status
        check (status in ('PASSED', 'FAILED')),
    constraint uq_shadow_validation_report_campaign
        unique (campaign_id)
);

create index ix_shadow_validation_reports_completed
    on capital_cipher.shadow_validation_reports (
        completed_at desc,
        report_id
    );

create or replace function
    capital_cipher.reject_shadow_validation_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'shadow validation evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_shadow_validation_mutation()
from public;

create trigger trg_shadow_campaign_checkpoints_immutable
before update or delete
on capital_cipher.shadow_campaign_checkpoints
for each row execute function
    capital_cipher.reject_shadow_validation_mutation();

create trigger trg_shadow_validation_reports_immutable
before update or delete
on capital_cipher.shadow_validation_reports
for each row execute function
    capital_cipher.reject_shadow_validation_mutation();

alter table capital_cipher.shadow_campaign_checkpoints
    enable row level security;
alter table capital_cipher.shadow_validation_reports
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

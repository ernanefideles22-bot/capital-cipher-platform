set lock_timeout = '5s';
set statement_timeout = '60s';

create schema if not exists capital_cipher;
revoke all on schema capital_cipher from public;

create table if not exists capital_cipher.agent_execution_jobs (
    execution_id varchar(64) primary key,
    request_fingerprint varchar(64) not null unique,
    schema_version varchar(16) not null,
    runtime_version varchar(32) not null,
    idempotency_key varchar(256) not null,
    correlation_id varchar(36) not null,
    agent_name varchar(128) not null,
    agent_version varchar(32) not null,
    agent_definition_hash varchar(64) not null,
    execution_mode varchar(16) not null,
    decision_role varchar(16) not null,
    critical boolean not null,
    input_payload jsonb not null,
    status varchar(16) not null,
    attempt_count integer not null,
    max_attempts integer not null,
    available_at timestamptz not null,
    leased_by varchar(128),
    lease_expires_at timestamptz,
    last_error_code varchar(64),
    output_payload jsonb,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    completed_at timestamptz,
    constraint uq_agent_execution_jobs_idempotency
        unique (agent_name, agent_version, idempotency_key),
    constraint ck_agent_execution_jobs_paper_only
        check (execution_mode = 'PAPER'),
    constraint ck_agent_execution_jobs_decision_role
        check (decision_role in ('PRIMARY', 'SHADOW')),
    constraint ck_agent_execution_jobs_status
        check (
            status in (
                'PENDING',
                'LEASED',
                'RETRY',
                'COMPLETED',
                'DEAD_LETTER'
            )
        ),
    constraint ck_agent_execution_jobs_attempts
        check (
            attempt_count >= 0
            and attempt_count <= max_attempts
            and max_attempts > 0
            and max_attempts <= 10
        ),
    constraint ck_agent_execution_jobs_lease
        check (
            (
                status = 'LEASED'
                and leased_by is not null
                and lease_expires_at is not null
            )
            or
            (
                status <> 'LEASED'
                and leased_by is null
                and lease_expires_at is null
            )
        ),
    constraint ck_agent_execution_jobs_terminal
        check (
            (
                status in ('COMPLETED', 'DEAD_LETTER')
                and completed_at is not null
            )
            or
            (
                status not in ('COMPLETED', 'DEAD_LETTER')
                and completed_at is null
            )
        )
);

create index if not exists ix_agent_execution_jobs_ready
    on capital_cipher.agent_execution_jobs (
        status,
        available_at,
        created_at
    )
    where status in ('PENDING', 'RETRY');

create index if not exists ix_agent_execution_jobs_expired_leases
    on capital_cipher.agent_execution_jobs (
        lease_expires_at,
        created_at
    )
    where status = 'LEASED';

create index if not exists ix_agent_execution_jobs_correlation
    on capital_cipher.agent_execution_jobs (
        correlation_id,
        created_at
    );

create table if not exists capital_cipher.agent_execution_attempts (
    row_id bigint generated always as identity primary key,
    execution_id varchar(64) not null
        references capital_cipher.agent_execution_jobs (execution_id)
        on delete restrict,
    schema_version varchar(16) not null,
    attempt_number integer not null,
    worker_id varchar(128) not null,
    status varchar(16) not null,
    output_payload jsonb not null,
    retryable boolean not null,
    started_at timestamptz not null,
    completed_at timestamptz not null,
    constraint uq_agent_execution_attempts_number
        unique (execution_id, attempt_number),
    constraint ck_agent_execution_attempts_number
        check (attempt_number > 0 and attempt_number <= 10),
    constraint ck_agent_execution_attempts_time
        check (completed_at >= started_at)
);

create index if not exists ix_agent_execution_attempts_execution
    on capital_cipher.agent_execution_attempts (
        execution_id,
        attempt_number
    );

create table if not exists capital_cipher.agent_memory_entries (
    row_id bigint generated always as identity primary key,
    execution_id varchar(64) not null
        references capital_cipher.agent_execution_jobs (execution_id)
        on delete restrict,
    schema_version varchar(16) not null,
    sequence integer not null,
    entry_type varchar(16) not null,
    payload jsonb not null,
    payload_hash varchar(64) not null,
    created_at timestamptz not null,
    constraint uq_agent_memory_entries_sequence
        unique (execution_id, sequence),
    constraint ck_agent_memory_entries_sequence
        check (sequence > 0),
    constraint ck_agent_memory_entries_type
        check (
            entry_type in (
                'INPUT',
                'ATTEMPT',
                'OUTPUT',
                'DEAD_LETTER'
            )
        )
);

create index if not exists ix_agent_memory_entries_execution
    on capital_cipher.agent_memory_entries (
        execution_id,
        sequence
    );

alter table capital_cipher.agent_execution_jobs
    enable row level security;
alter table capital_cipher.agent_execution_attempts
    enable row level security;
alter table capital_cipher.agent_memory_entries
    enable row level security;

create or replace function
    capital_cipher.reject_agent_evidence_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'agent runtime evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_agent_evidence_mutation()
from public;

do $block$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgname = 'trg_agent_execution_attempts_immutable'
          and tgrelid =
              'capital_cipher.agent_execution_attempts'::regclass
    ) then
        create trigger trg_agent_execution_attempts_immutable
        before update or delete
        on capital_cipher.agent_execution_attempts
        for each row
        execute function
            capital_cipher.reject_agent_evidence_mutation();
    end if;

    if not exists (
        select 1
        from pg_trigger
        where tgname = 'trg_agent_memory_entries_immutable'
          and tgrelid =
              'capital_cipher.agent_memory_entries'::regclass
    ) then
        create trigger trg_agent_memory_entries_immutable
        before update or delete
        on capital_cipher.agent_memory_entries
        for each row
        execute function
            capital_cipher.reject_agent_evidence_mutation();
    end if;
end;
$block$;

revoke all on table
    capital_cipher.agent_execution_jobs,
    capital_cipher.agent_execution_attempts,
    capital_cipher.agent_memory_entries
from public;

revoke all on sequence
    capital_cipher.agent_execution_attempts_row_id_seq,
    capital_cipher.agent_memory_entries_row_id_seq
from public;

comment on table capital_cipher.agent_execution_jobs is
    'Durable, idempotent PAPER-only queue for governed agent work.';
comment on table capital_cipher.agent_execution_attempts is
    'Append-only evidence for bounded agent execution attempts.';
comment on table capital_cipher.agent_memory_entries is
    'Append-only memory scoped to one agent execution identity.';

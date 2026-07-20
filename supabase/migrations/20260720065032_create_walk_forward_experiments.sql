set lock_timeout = '5s';
set statement_timeout = '60s';

create schema if not exists capital_cipher;
revoke all on schema capital_cipher from public;

create table if not exists capital_cipher.walk_forward_experiments (
    row_id bigint generated always as identity primary key,
    experiment_id varchar(80) not null unique,
    artifact_hash varchar(64) not null unique,
    schema_version varchar(16) not null,
    artifact_version varchar(32) not null,
    protocol_version varchar(32) not null,
    dataset_id varchar(96) not null,
    dataset_hash varchar(64) not null,
    symbol text not null,
    timeframe text not null,
    candidate_version text not null,
    promotion_status varchar(16) not null,
    report_payload jsonb not null,
    created_at timestamptz not null,
    recorded_at timestamptz not null,
    constraint ck_walk_forward_experiments_research_only
        check (promotion_status = 'RESEARCH_ONLY'),
    constraint ck_walk_forward_experiments_artifact_version
        check (artifact_version = 'walk-forward-artifact-v1'),
    constraint ck_walk_forward_experiments_dataset_hash
        check (length(dataset_hash) = 64),
    constraint ck_walk_forward_experiments_artifact_hash
        check (length(artifact_hash) = 64)
);

create index if not exists
    ix_walk_forward_experiments_candidate_created
    on capital_cipher.walk_forward_experiments (
        candidate_version,
        created_at
    );

create index if not exists
    ix_walk_forward_experiments_dataset_created
    on capital_cipher.walk_forward_experiments (
        dataset_hash,
        created_at
    );

alter table capital_cipher.walk_forward_experiments
    enable row level security;

create or replace function
    capital_cipher.reject_walk_forward_experiment_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'walk_forward_experiments is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_walk_forward_experiment_mutation()
from public;

do $block$
begin
    if not exists (
        select 1
        from pg_trigger
        where tgname = 'trg_walk_forward_experiments_immutable'
          and tgrelid =
              'capital_cipher.walk_forward_experiments'::regclass
    ) then
        create trigger trg_walk_forward_experiments_immutable
        before update or delete
        on capital_cipher.walk_forward_experiments
        for each row
        execute function
            capital_cipher.reject_walk_forward_experiment_mutation();
    end if;
end;
$block$;

revoke all on table
    capital_cipher.walk_forward_experiments
from public;

revoke all on sequence
    capital_cipher.walk_forward_experiments_row_id_seq
from public;

comment on table capital_cipher.walk_forward_experiments is
    'Append-only, content-addressed RESEARCH_ONLY walk-forward artifacts.';

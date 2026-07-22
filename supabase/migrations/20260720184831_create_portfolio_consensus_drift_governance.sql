set lock_timeout = '5s';
set statement_timeout = '60s';

create table capital_cipher.consensus_experiments (
    experiment_id varchar(64) primary key,
    schema_version varchar(16) not null,
    name varchar(64) not null,
    version varchar(32) not null,
    mode varchar(16) not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint uq_consensus_experiment_name_version
        unique (name, version),
    constraint ck_consensus_experiment_id
        check (experiment_id ~ '^[a-f0-9]{64}$'),
    constraint ck_consensus_experiment_mode
        check (mode in ('SHADOW', 'CONFIRMATION')),
    constraint ck_consensus_experiment_payload
        check (
            jsonb_typeof(payload) = 'object'
            and payload ->> 'experiment_id' = experiment_id
            and payload ->> 'name' = name
            and payload ->> 'version' = version
            and payload ->> 'mode' = mode
        )
);

create index ix_consensus_experiments_created
    on capital_cipher.consensus_experiments (
        created_at desc,
        experiment_id
    );

create table capital_cipher.consensus_experiment_events (
    event_id varchar(64) primary key,
    schema_version varchar(16) not null,
    experiment_id varchar(64) not null
        references capital_cipher.consensus_experiments (experiment_id)
        on delete restrict,
    event_type varchar(16) not null,
    actor varchar(128) not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint ck_consensus_experiment_event_id
        check (event_id ~ '^[a-f0-9]{64}$'),
    constraint ck_consensus_experiment_event_type
        check (event_type in ('CREATED', 'ACTIVATED', 'RETIRED')),
    constraint ck_consensus_experiment_event_payload
        check (
            jsonb_typeof(payload) = 'object'
            and payload ->> 'event_id' = event_id
            and payload ->> 'experiment_id' = experiment_id
            and payload ->> 'event_type' = event_type
        )
);

create index ix_consensus_experiment_events_experiment_created
    on capital_cipher.consensus_experiment_events (
        experiment_id,
        created_at desc
    );

create table capital_cipher.weighted_consensus_snapshots (
    consensus_id varchar(64) primary key,
    schema_version varchar(16) not null,
    correlation_id varchar(36) not null,
    experiment_id varchar(64) not null
        references capital_cipher.consensus_experiments (experiment_id)
        on delete restrict,
    symbol varchar(32) not null,
    timeframe varchar(16) not null,
    status varchar(24) not null,
    eligible_agent_count integer not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint ck_weighted_consensus_id
        check (consensus_id ~ '^[a-f0-9]{64}$'),
    constraint ck_weighted_consensus_status
        check (status in ('INSUFFICIENT_DATA', 'READY')),
    constraint ck_weighted_consensus_eligible
        check (eligible_agent_count between 0 and 150),
    constraint ck_weighted_consensus_payload
        check (
            jsonb_typeof(payload) = 'object'
            and payload ->> 'consensus_id' = consensus_id
            and payload ->> 'experiment_id' = experiment_id
            and payload ->> 'status' = status
        )
);

create index ix_weighted_consensus_symbol_created
    on capital_cipher.weighted_consensus_snapshots (
        symbol,
        timeframe,
        created_at desc
    );
create index ix_weighted_consensus_experiment_created
    on capital_cipher.weighted_consensus_snapshots (
        experiment_id,
        created_at desc
    );

create table capital_cipher.drift_observations (
    observation_id varchar(64) primary key,
    schema_version varchar(16) not null,
    experiment_id varchar(64) not null
        references capital_cipher.consensus_experiments (experiment_id)
        on delete restrict,
    agent_name varchar(128) not null,
    agent_version varchar(32) not null,
    severity varchar(16) not null,
    payload jsonb not null,
    observed_at timestamptz not null,
    created_at timestamptz not null,
    constraint ck_drift_observation_id
        check (observation_id ~ '^[a-f0-9]{64}$'),
    constraint ck_drift_observation_severity
        check (severity in ('NONE', 'WARNING', 'CRITICAL')),
    constraint ck_drift_observation_payload
        check (
            jsonb_typeof(payload) = 'object'
            and payload ->> 'observation_id' = observation_id
            and payload ->> 'experiment_id' = experiment_id
            and payload ->> 'agent_name' = agent_name
            and payload ->> 'severity' = severity
        )
);

create index ix_drift_observations_agent_observed
    on capital_cipher.drift_observations (
        agent_name,
        agent_version,
        observed_at desc
    );
create index ix_drift_observations_experiment_severity
    on capital_cipher.drift_observations (
        experiment_id,
        severity
    );

create table capital_cipher.portfolio_proposals (
    proposal_id varchar(64) primary key,
    schema_version varchar(16) not null,
    correlation_id varchar(36) not null,
    consensus_id varchar(64)
        references capital_cipher.weighted_consensus_snapshots (consensus_id)
        on delete restrict,
    experiment_id varchar(64) not null
        references capital_cipher.consensus_experiments (experiment_id)
        on delete restrict,
    symbol varchar(32) not null,
    timeframe varchar(16) not null,
    status varchar(16) not null,
    max_notional numeric(38, 18) not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint ck_portfolio_proposal_id
        check (proposal_id ~ '^[a-f0-9]{64}$'),
    constraint ck_portfolio_proposal_status
        check (status in ('NO_ACTION', 'PROPOSED', 'BLOCKED')),
    constraint ck_portfolio_proposal_notional
        check (
            max_notional >= 0
            and (
                (status = 'PROPOSED' and max_notional > 0)
                or (status <> 'PROPOSED' and max_notional = 0)
            )
        ),
    constraint ck_portfolio_proposal_payload
        check (
            jsonb_typeof(payload) = 'object'
            and payload ->> 'proposal_id' = proposal_id
            and payload ->> 'experiment_id' = experiment_id
            and payload ->> 'status' = status
        )
);

create index ix_portfolio_proposals_symbol_created
    on capital_cipher.portfolio_proposals (
        symbol,
        timeframe,
        created_at desc
    );
create index ix_portfolio_proposals_consensus
    on capital_cipher.portfolio_proposals (consensus_id);
create index ix_portfolio_proposals_experiment
    on capital_cipher.portfolio_proposals (experiment_id);

create or replace function
    capital_cipher.reject_portfolio_consensus_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'portfolio and consensus evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_portfolio_consensus_mutation()
from public;

create trigger trg_consensus_experiments_immutable
before update or delete on capital_cipher.consensus_experiments
for each row execute function
    capital_cipher.reject_portfolio_consensus_mutation();
create trigger trg_consensus_experiment_events_immutable
before update or delete on capital_cipher.consensus_experiment_events
for each row execute function
    capital_cipher.reject_portfolio_consensus_mutation();
create trigger trg_weighted_consensus_snapshots_immutable
before update or delete on capital_cipher.weighted_consensus_snapshots
for each row execute function
    capital_cipher.reject_portfolio_consensus_mutation();
create trigger trg_drift_observations_immutable
before update or delete on capital_cipher.drift_observations
for each row execute function
    capital_cipher.reject_portfolio_consensus_mutation();
create trigger trg_portfolio_proposals_immutable
before update or delete on capital_cipher.portfolio_proposals
for each row execute function
    capital_cipher.reject_portfolio_consensus_mutation();

alter table capital_cipher.consensus_experiments
    enable row level security;
alter table capital_cipher.consensus_experiment_events
    enable row level security;
alter table capital_cipher.weighted_consensus_snapshots
    enable row level security;
alter table capital_cipher.drift_observations
    enable row level security;
alter table capital_cipher.portfolio_proposals
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

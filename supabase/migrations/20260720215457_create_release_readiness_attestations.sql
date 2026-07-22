-- Month 12 release-readiness evidence and fail-closed TESTNET gates.
-- Private direct-Postgres schema; no browser/Data API access is granted.

create table capital_cipher.release_evidence_bundles (
    evidence_bundle_id varchar(36) primary key,
    schema_version varchar(16) not null,
    source_revision varchar(40) not null,
    bundle_sha256 varchar(64) not null,
    status varchar(16) not null,
    payload jsonb not null,
    collected_at timestamptz not null,
    constraint ck_release_evidence_status
        check (status in ('PASSED', 'FAILED')),
    constraint ck_release_evidence_source_revision
        check (source_revision ~ '^[a-f0-9]{40}$'),
    constraint ck_release_evidence_bundle_hash
        check (bundle_sha256 ~ '^[a-f0-9]{64}$')
);

create index ix_release_evidence_collected
    on capital_cipher.release_evidence_bundles (
        collected_at desc,
        evidence_bundle_id
    );

create table capital_cipher.independent_audit_attestations (
    attestation_id varchar(36) primary key,
    schema_version varchar(16) not null,
    evidence_bundle_id varchar(36) not null,
    source_revision varchar(40) not null,
    decision varchar(24) not null,
    payload jsonb not null,
    issued_at timestamptz not null,
    expires_at timestamptz not null,
    constraint ck_independent_audit_decision
        check (decision in ('APPROVED_TESTNET', 'REJECTED')),
    constraint ck_independent_audit_validity
        check (
            expires_at > issued_at
            and expires_at <= issued_at + interval '30 days'
        ),
    constraint ck_independent_audit_no_live
        check (((payload ->> 'live_execution_authorized')::boolean) is false)
);

create index ix_independent_audit_issued
    on capital_cipher.independent_audit_attestations (
        issued_at desc,
        attestation_id
    );

create table capital_cipher.testnet_canary_drill_reports (
    drill_id varchar(36) primary key,
    schema_version varchar(16) not null,
    evidence_bundle_id varchar(36) not null,
    attestation_id varchar(36) not null,
    status varchar(16) not null,
    payload jsonb not null,
    completed_at timestamptz not null,
    constraint ck_testnet_canary_drill_status
        check (status in ('PASSED', 'FAILED')),
    constraint ck_testnet_canary_no_remote_call
        check (((payload ->> 'remote_api_call_attempted')::boolean) is false),
    constraint ck_testnet_canary_no_real_funds
        check (((payload ->> 'real_funds_used')::boolean) is false),
    constraint ck_testnet_canary_no_live
        check (((payload ->> 'live_execution_attempted')::boolean) is false)
);

create index ix_testnet_canary_completed
    on capital_cipher.testnet_canary_drill_reports (
        completed_at desc,
        drill_id
    );

create table capital_cipher.release_gate_decisions (
    gate_decision_id varchar(36) primary key,
    schema_version varchar(16) not null,
    evidence_bundle_id varchar(36) not null,
    source_revision varchar(40) not null,
    outcome varchar(40) not null,
    testnet_release_authorized boolean not null,
    live_execution_authorized boolean not null default false,
    payload jsonb not null,
    decided_at timestamptz not null,
    constraint ck_release_gate_outcome
        check (
            outcome in (
                'APPROVED_TESTNET',
                'BLOCKED_PENDING_EXTERNAL_AUDIT',
                'BLOCKED_TECHNICAL'
            )
        ),
    constraint ck_release_gate_authorization
        check (
            testnet_release_authorized = (outcome = 'APPROVED_TESTNET')
        ),
    constraint ck_release_gate_no_live
        check (live_execution_authorized = false)
);

create index ix_release_gate_decided
    on capital_cipher.release_gate_decisions (
        decided_at desc,
        gate_decision_id
    );

create or replace function
    capital_cipher.reject_release_readiness_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'release readiness evidence is append-only'
        using errcode = '55000';
end;
$function$;

revoke all on function
    capital_cipher.reject_release_readiness_mutation()
from public;

create trigger trg_release_evidence_bundles_immutable
before update or delete
on capital_cipher.release_evidence_bundles
for each row execute function
    capital_cipher.reject_release_readiness_mutation();

create trigger trg_independent_audit_attestations_immutable
before update or delete
on capital_cipher.independent_audit_attestations
for each row execute function
    capital_cipher.reject_release_readiness_mutation();

create trigger trg_testnet_canary_drill_reports_immutable
before update or delete
on capital_cipher.testnet_canary_drill_reports
for each row execute function
    capital_cipher.reject_release_readiness_mutation();

create trigger trg_release_gate_decisions_immutable
before update or delete
on capital_cipher.release_gate_decisions
for each row execute function
    capital_cipher.reject_release_readiness_mutation();

alter table capital_cipher.release_evidence_bundles
    enable row level security;
alter table capital_cipher.independent_audit_attestations
    enable row level security;
alter table capital_cipher.testnet_canary_drill_reports
    enable row level security;
alter table capital_cipher.release_gate_decisions
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

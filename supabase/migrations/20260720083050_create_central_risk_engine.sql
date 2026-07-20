set lock_timeout = '5s';
set statement_timeout = '60s';

create schema if not exists capital_cipher;
revoke all on schema capital_cipher from public;

create table if not exists capital_cipher.risk_evaluations (
    evaluation_id varchar(64) primary key,
    risk_check_id varchar(36) not null unique,
    idempotency_key text not null unique,
    request_fingerprint varchar(64) not null,
    decision_id varchar(36) not null,
    correlation_id varchar(36) not null,
    risk_status varchar(16) not null,
    approved boolean not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint ck_risk_evaluation_id
        check (length(evaluation_id) = 64),
    constraint ck_risk_evaluation_fingerprint
        check (length(request_fingerprint) = 64),
    constraint ck_risk_evaluation_status
        check (
            risk_status in (
                'APPROVED',
                'REDUCED',
                'BLOCKED',
                'KILL_SWITCH'
            )
        ),
    constraint ck_risk_evaluation_approval
        check (
            approved =
                (risk_status in ('APPROVED', 'REDUCED'))
        )
);

create index if not exists ix_risk_evaluations_decision_created
    on capital_cipher.risk_evaluations (decision_id, created_at);

create table if not exists capital_cipher.order_approvals (
    approval_id varchar(64) primary key,
    evaluation_id varchar(64) not null unique
        references capital_cipher.risk_evaluations (evaluation_id)
        on delete restrict,
    risk_check_id varchar(36) not null,
    decision_id varchar(36) not null,
    correlation_id varchar(36) not null,
    request_fingerprint varchar(64) not null,
    position_snapshot_hash varchar(64) not null,
    symbol text not null,
    timeframe text not null,
    strategy text not null,
    side varchar(4) not null,
    max_notional numeric(38, 18) not null,
    max_leverage numeric(20, 8) not null,
    reference_price numeric(38, 18) not null,
    max_entry_deviation_bps numeric(20, 8) not null,
    status varchar(16) not null,
    created_at timestamptz not null,
    expires_at timestamptz not null,
    consumed_at timestamptz,
    paper_order_id varchar(36) unique,
    constraint ck_order_approval_id
        check (length(approval_id) = 64),
    constraint ck_order_approval_fingerprint
        check (length(request_fingerprint) = 64),
    constraint ck_order_approval_position_snapshot
        check (length(position_snapshot_hash) = 64),
    constraint ck_order_approval_status
        check (status in ('ACTIVE', 'CONSUMED', 'REVOKED', 'EXPIRED')),
    constraint ck_order_approval_side
        check (side in ('BUY', 'SELL')),
    constraint ck_order_approval_notional
        check (max_notional > 0),
    constraint ck_order_approval_leverage
        check (max_leverage >= 1),
    constraint ck_order_approval_price
        check (reference_price > 0),
    constraint ck_order_approval_deviation
        check (max_entry_deviation_bps >= 0),
    constraint ck_order_approval_expiry
        check (expires_at > created_at),
    constraint ck_order_approval_terminal
        check (
            (
                status = 'CONSUMED'
                and consumed_at is not null
                and paper_order_id is not null
            )
            or
            (
                status <> 'CONSUMED'
                and consumed_at is null
                and paper_order_id is null
            )
        )
);

create index if not exists ix_order_approvals_active_expiry
    on capital_cipher.order_approvals (expires_at)
    where status = 'ACTIVE';

create table if not exists capital_cipher.risk_control_state (
    singleton_id integer primary key default 1,
    active boolean not null default false,
    revision bigint not null default 0,
    reason text,
    actor text,
    triggered_at timestamptz,
    reset_at timestamptz,
    updated_at timestamptz not null,
    constraint ck_risk_control_singleton
        check (singleton_id = 1),
    constraint ck_risk_control_revision
        check (revision >= 0)
);

insert into capital_cipher.risk_control_state (
    singleton_id,
    active,
    revision,
    updated_at
)
values (1, false, 0, now())
on conflict (singleton_id) do nothing;

create table if not exists capital_cipher.risk_control_events (
    event_id varchar(36) primary key,
    revision bigint not null unique,
    event_type varchar(16) not null,
    reason text not null,
    actor text not null,
    correlation_id varchar(36),
    created_at timestamptz not null,
    constraint ck_risk_control_event_type
        check (event_type in ('TRIGGERED', 'RESET')),
    constraint ck_risk_control_event_revision
        check (revision > 0)
);

create index if not exists ix_risk_control_events_created
    on capital_cipher.risk_control_events (created_at);

alter table capital_cipher.risk_evaluations enable row level security;
alter table capital_cipher.order_approvals enable row level security;
alter table capital_cipher.risk_control_state enable row level security;
alter table capital_cipher.risk_control_events enable row level security;

create or replace function
    capital_cipher.reject_central_risk_evidence_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'central risk evidence is append-only'
        using errcode = '55000';
end;
$function$;

create or replace function
    capital_cipher.guard_order_approval_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if old.approval_id <> new.approval_id
       or old.evaluation_id <> new.evaluation_id
       or old.risk_check_id <> new.risk_check_id
       or old.decision_id <> new.decision_id
       or old.request_fingerprint <> new.request_fingerprint
       or old.position_snapshot_hash <> new.position_snapshot_hash
       or old.symbol <> new.symbol
       or old.timeframe <> new.timeframe
       or old.strategy <> new.strategy
       or old.side <> new.side
       or old.max_notional <> new.max_notional
       or old.max_leverage <> new.max_leverage
       or old.reference_price <> new.reference_price
       or old.max_entry_deviation_bps <> new.max_entry_deviation_bps
       or old.created_at <> new.created_at
       or old.expires_at <> new.expires_at then
        raise exception 'order approval identity is immutable'
            using errcode = '55000';
    end if;
    if old.status <> 'ACTIVE'
       or new.status not in ('CONSUMED', 'REVOKED', 'EXPIRED') then
        raise exception 'invalid order approval transition'
            using errcode = '55000';
    end if;
    return new;
end;
$function$;

create or replace function
    capital_cipher.guard_risk_control_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if old.singleton_id <> new.singleton_id
       or new.revision <> old.revision + 1
       or new.active = old.active then
        raise exception 'invalid risk control transition'
            using errcode = '55000';
    end if;
    return new;
end;
$function$;

revoke all on function
    capital_cipher.reject_central_risk_evidence_mutation()
from public;
revoke all on function
    capital_cipher.guard_order_approval_transition()
from public;
revoke all on function
    capital_cipher.guard_risk_control_transition()
from public;

do $block$
begin
    if not exists (
        select 1 from pg_trigger
        where tgname = 'trg_risk_evaluations_immutable'
          and tgrelid = 'capital_cipher.risk_evaluations'::regclass
    ) then
        create trigger trg_risk_evaluations_immutable
        before update or delete
        on capital_cipher.risk_evaluations
        for each row execute function
            capital_cipher.reject_central_risk_evidence_mutation();
    end if;
    if not exists (
        select 1 from pg_trigger
        where tgname = 'trg_risk_control_events_immutable'
          and tgrelid = 'capital_cipher.risk_control_events'::regclass
    ) then
        create trigger trg_risk_control_events_immutable
        before update or delete
        on capital_cipher.risk_control_events
        for each row execute function
            capital_cipher.reject_central_risk_evidence_mutation();
    end if;
    if not exists (
        select 1 from pg_trigger
        where tgname = 'trg_order_approval_transition'
          and tgrelid = 'capital_cipher.order_approvals'::regclass
    ) then
        create trigger trg_order_approval_transition
        before update
        on capital_cipher.order_approvals
        for each row execute function
            capital_cipher.guard_order_approval_transition();
    end if;
    if not exists (
        select 1 from pg_trigger
        where tgname = 'trg_risk_control_transition'
          and tgrelid = 'capital_cipher.risk_control_state'::regclass
    ) then
        create trigger trg_risk_control_transition
        before update
        on capital_cipher.risk_control_state
        for each row execute function
            capital_cipher.guard_risk_control_transition();
    end if;
end;
$block$;

do $block$
begin
    if to_regclass('public.paper_orders') is not null then
        alter table public.paper_orders
            add column if not exists approval_id varchar(64),
            add column if not exists request_fingerprint varchar(64),
            add column if not exists timeframe text,
            add column if not exists strategy text not null
                default 'UNSPECIFIED',
            add column if not exists leverage numeric not null default 1;
        create unique index if not exists
            uq_paper_orders_approval_id
            on public.paper_orders (approval_id)
            where approval_id is not null;
    end if;
end;
$block$;

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

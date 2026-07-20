set lock_timeout = '5s';
set statement_timeout = '60s';

alter table capital_cipher.order_approvals
    add column if not exists oms_order_id varchar(36);

create unique index if not exists uq_order_approvals_oms_order_id
    on capital_cipher.order_approvals (oms_order_id)
    where oms_order_id is not null;

alter table capital_cipher.order_approvals
    drop constraint if exists ck_order_approval_terminal;

alter table capital_cipher.order_approvals
    add constraint ck_order_approval_terminal
    check (
        (
            status = 'CONSUMED'
            and consumed_at is not null
            and (
                (
                    paper_order_id is not null
                    and oms_order_id is null
                )
                or (
                    paper_order_id is null
                    and oms_order_id is not null
                )
            )
        )
        or (
            status <> 'CONSUMED'
            and consumed_at is null
            and paper_order_id is null
            and oms_order_id is null
        )
    );

create table capital_cipher.oms_orders (
    oms_order_id varchar(36) primary key,
    client_order_id varchar(36) not null,
    decision_id varchar(36) not null,
    risk_check_id varchar(36) not null,
    approval_id varchar(64) not null unique
        references capital_cipher.order_approvals (approval_id)
        on delete restrict,
    request_fingerprint varchar(64) not null,
    correlation_id varchar(36) not null,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    symbol text not null,
    timeframe text not null,
    strategy text not null,
    side varchar(4) not null,
    order_type varchar(16) not null,
    time_in_force varchar(16) not null,
    quantity numeric(38, 18) not null,
    requested_notional numeric(38, 18) not null,
    leverage numeric(20, 8) not null,
    limit_price numeric(38, 18),
    reference_price numeric(38, 18) not null,
    status varchar(24) not null,
    venue_order_id text,
    cumulative_filled_quantity numeric(38, 18) not null default 0,
    average_fill_price numeric(38, 18),
    rejection_reason text,
    state_version integer not null default 1,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    submitted_at timestamptz,
    terminal_at timestamptz,
    constraint uq_oms_order_client_identity
        unique (exchange, environment, client_order_id),
    constraint ck_oms_order_environment
        check (environment in ('PAPER', 'TESTNET')),
    constraint ck_oms_order_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_oms_order_side
        check (side in ('BUY', 'SELL')),
    constraint ck_oms_order_type
        check (order_type in ('MARKET', 'LIMIT')),
    constraint ck_oms_order_time_in_force
        check (time_in_force in ('GTC', 'IOC', 'FOK', 'POST_ONLY')),
    constraint ck_oms_order_status
        check (
            status in (
                'CREATED', 'PENDING_SUBMISSION', 'SUBMITTED',
                'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING',
                'CANCELED', 'REJECTED', 'EXPIRED', 'UNKNOWN',
                'QUARANTINED'
            )
        ),
    constraint ck_oms_order_quantity check (quantity > 0),
    constraint ck_oms_order_notional check (requested_notional > 0),
    constraint ck_oms_order_reference check (reference_price > 0),
    constraint ck_oms_order_leverage check (leverage >= 1),
    constraint ck_oms_order_version check (state_version >= 1),
    constraint ck_oms_order_filled_quantity
        check (
            cumulative_filled_quantity >= 0
            and cumulative_filled_quantity <= quantity
        ),
    constraint ck_oms_order_limit_price
        check (
            (order_type = 'LIMIT' and limit_price is not null)
            or order_type = 'MARKET'
        ),
    constraint ck_oms_order_terminal
        check (
            (
                status in (
                    'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED',
                    'QUARANTINED'
                )
                and terminal_at is not null
            )
            or (
                status not in (
                    'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED',
                    'QUARANTINED'
                )
                and terminal_at is null
            )
        )
);

create index ix_oms_orders_active_updated
    on capital_cipher.oms_orders (
        exchange,
        environment,
        updated_at
    )
    where status in (
        'CREATED', 'PENDING_SUBMISSION', 'SUBMITTED',
        'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN'
    );

create index ix_oms_orders_venue_order
    on capital_cipher.oms_orders (venue_order_id);

create table capital_cipher.oms_order_events (
    event_id varchar(36) primary key,
    oms_order_id varchar(36) not null
        references capital_cipher.oms_orders (oms_order_id)
        on delete restrict,
    state_version integer not null,
    event_type varchar(48) not null,
    status varchar(24) not null,
    payload jsonb not null,
    created_at timestamptz not null,
    constraint uq_oms_order_event_version
        unique (oms_order_id, state_version)
);

create index ix_oms_order_events_order_created
    on capital_cipher.oms_order_events (oms_order_id, created_at);

create table capital_cipher.execution_commands (
    command_id varchar(36) primary key,
    oms_order_id varchar(36) not null
        references capital_cipher.oms_orders (oms_order_id)
        on delete restrict,
    command_type varchar(16) not null,
    status varchar(16) not null,
    attempt_count integer not null,
    max_attempts integer not null,
    leased_by varchar(64),
    lease_expires_at timestamptz,
    available_at timestamptz not null,
    last_error_type varchar(128),
    created_at timestamptz not null,
    completed_at timestamptz,
    constraint uq_execution_command_order_type
        unique (oms_order_id, command_type),
    constraint ck_execution_command_type
        check (command_type in ('SUBMIT', 'CANCEL')),
    constraint ck_execution_command_status
        check (status in ('PENDING', 'LEASED', 'COMPLETED', 'DEAD_LETTER')),
    constraint ck_execution_command_attempts
        check (
            attempt_count >= 0
            and max_attempts = 1
            and attempt_count <= max_attempts
        ),
    constraint ck_execution_command_lifecycle
        check (
            (
                status = 'PENDING'
                and leased_by is null
                and lease_expires_at is null
                and completed_at is null
            )
            or (
                status = 'LEASED'
                and leased_by is not null
                and lease_expires_at is not null
                and completed_at is null
            )
            or (
                status in ('COMPLETED', 'DEAD_LETTER')
                and leased_by is null
                and lease_expires_at is null
                and completed_at is not null
            )
        )
);

create index ix_execution_commands_claimable
    on capital_cipher.execution_commands (available_at, created_at)
    where status = 'PENDING'
       or (status = 'LEASED' and lease_expires_at is not null);

create table capital_cipher.execution_fills (
    fill_id text primary key,
    oms_order_id varchar(36)
        references capital_cipher.oms_orders (oms_order_id)
        on delete restrict,
    venue_order_id text not null,
    client_order_id varchar(36),
    exchange varchar(16) not null,
    environment varchar(16) not null,
    symbol text not null,
    side varchar(4) not null,
    quantity numeric(38, 18) not null,
    price numeric(38, 18) not null,
    fee numeric(38, 18) not null,
    fee_asset text,
    occurred_at timestamptz not null,
    observed_at timestamptz not null,
    constraint ck_execution_fill_quantity check (quantity > 0),
    constraint ck_execution_fill_price check (price > 0),
    constraint ck_execution_fill_fee check (fee >= 0),
    constraint ck_execution_fill_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_execution_fill_environment
        check (environment in ('PAPER', 'TESTNET')),
    constraint ck_execution_fill_side
        check (side in ('BUY', 'SELL'))
);

create index ix_execution_fills_order_time
    on capital_cipher.execution_fills (oms_order_id, occurred_at);
create index ix_execution_fills_venue_order
    on capital_cipher.execution_fills (venue_order_id);

create table capital_cipher.reconciliation_runs (
    run_id varchar(36) primary key,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    status varchar(16) not null,
    local_order_count integer not null,
    venue_order_count integer not null,
    fill_count integer not null,
    position_count integer not null,
    balance_count integer not null,
    mismatch_count integer not null,
    critical_mismatch_count integer not null,
    started_at timestamptz not null,
    completed_at timestamptz not null,
    error_type varchar(128),
    constraint ck_reconciliation_run_status
        check (status in ('MATCHED', 'DRIFT', 'FAILED')),
    constraint ck_reconciliation_run_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_reconciliation_run_environment
        check (environment in ('PAPER', 'TESTNET')),
    constraint ck_reconciliation_run_counts
        check (
            local_order_count >= 0
            and venue_order_count >= 0
            and fill_count >= 0
            and position_count >= 0
            and balance_count >= 0
            and mismatch_count >= 0
            and critical_mismatch_count >= 0
            and critical_mismatch_count <= mismatch_count
        ),
    constraint ck_reconciliation_run_error
        check (
            (status = 'FAILED' and error_type is not null)
            or (status <> 'FAILED' and error_type is null)
        )
);

create index ix_reconciliation_runs_venue_completed
    on capital_cipher.reconciliation_runs (
        exchange,
        environment,
        completed_at
    );

create table capital_cipher.reconciliation_mismatches (
    mismatch_id varchar(36) primary key,
    run_id varchar(36) not null
        references capital_cipher.reconciliation_runs (run_id)
        on delete restrict,
    mismatch_type varchar(48) not null,
    severity varchar(16) not null,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    oms_order_id varchar(36),
    venue_order_id text,
    symbol text,
    expected jsonb not null,
    observed jsonb not null,
    created_at timestamptz not null,
    constraint ck_reconciliation_mismatch_type
        check (
            mismatch_type in (
                'LOCAL_ORDER_MISSING_AT_VENUE',
                'ORPHAN_VENUE_ORDER',
                'ORPHAN_VENUE_FILL',
                'ORDER_STATUS_DRIFT',
                'FILLED_QUANTITY_DRIFT',
                'POSITION_QUANTITY_DRIFT',
                'ADAPTER_UNAVAILABLE'
            )
        ),
    constraint ck_reconciliation_mismatch_severity
        check (severity in ('INFO', 'WARNING', 'CRITICAL')),
    constraint ck_reconciliation_mismatch_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_reconciliation_mismatch_environment
        check (environment in ('PAPER', 'TESTNET'))
);

create index ix_reconciliation_mismatches_run_severity
    on capital_cipher.reconciliation_mismatches (run_id, severity);

create table capital_cipher.venue_position_snapshots (
    snapshot_id varchar(36) primary key,
    run_id varchar(36) not null
        references capital_cipher.reconciliation_runs (run_id)
        on delete restrict,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    symbol text not null,
    side varchar(4) not null,
    quantity numeric(38, 18) not null,
    entry_price numeric(38, 18),
    mark_price numeric(38, 18),
    unrealized_pnl numeric(38, 18) not null,
    observed_at timestamptz not null,
    constraint ck_venue_position_quantity check (quantity >= 0),
    constraint ck_venue_position_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_venue_position_environment
        check (environment in ('PAPER', 'TESTNET')),
    constraint ck_venue_position_side
        check (side in ('BUY', 'SELL'))
);

create index ix_venue_position_snapshots_run
    on capital_cipher.venue_position_snapshots (run_id, symbol, side);

create table capital_cipher.venue_balance_snapshots (
    snapshot_id varchar(36) primary key,
    run_id varchar(36) not null
        references capital_cipher.reconciliation_runs (run_id)
        on delete restrict,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    asset text not null,
    available numeric(38, 18) not null,
    locked numeric(38, 18) not null,
    equity numeric(38, 18) not null,
    observed_at timestamptz not null,
    constraint ck_venue_balance_nonnegative
        check (available >= 0 and locked >= 0 and equity >= 0),
    constraint ck_venue_balance_exchange
        check (exchange in ('BINANCE', 'BYBIT')),
    constraint ck_venue_balance_environment
        check (environment in ('PAPER', 'TESTNET'))
);

create index ix_venue_balance_snapshots_run_asset
    on capital_cipher.venue_balance_snapshots (run_id, asset);

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
       or old.correlation_id <> new.correlation_id
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

create or replace function capital_cipher.reject_oms_evidence_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'OMS evidence is append-only'
        using errcode = '55000';
end;
$function$;

create or replace function capital_cipher.guard_oms_order_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception 'OMS orders cannot be deleted'
            using errcode = '55000';
    end if;
    if old.oms_order_id is distinct from new.oms_order_id
       or old.client_order_id is distinct from new.client_order_id
       or old.decision_id is distinct from new.decision_id
       or old.risk_check_id is distinct from new.risk_check_id
       or old.approval_id is distinct from new.approval_id
       or old.request_fingerprint is distinct from new.request_fingerprint
       or old.correlation_id is distinct from new.correlation_id
       or old.exchange is distinct from new.exchange
       or old.environment is distinct from new.environment
       or old.symbol is distinct from new.symbol
       or old.timeframe is distinct from new.timeframe
       or old.strategy is distinct from new.strategy
       or old.side is distinct from new.side
       or old.order_type is distinct from new.order_type
       or old.time_in_force is distinct from new.time_in_force
       or old.quantity is distinct from new.quantity
       or old.requested_notional is distinct from new.requested_notional
       or old.leverage is distinct from new.leverage
       or old.limit_price is distinct from new.limit_price
       or old.reference_price is distinct from new.reference_price
       or old.created_at is distinct from new.created_at then
        raise exception 'OMS order identity is immutable'
            using errcode = '55000';
    end if;
    if new.state_version <> old.state_version + 1 then
        raise exception 'invalid OMS state version'
            using errcode = '55000';
    end if;
    if old.status in (
        'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED', 'QUARANTINED'
    ) or not (
        new.status = old.status
        or (
            old.status in (
                'CREATED', 'PENDING_SUBMISSION', 'SUBMITTED',
                'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNKNOWN'
            )
            and new.status in (
                'PENDING_SUBMISSION', 'SUBMITTED', 'PARTIALLY_FILLED',
                'FILLED', 'CANCEL_PENDING', 'CANCELED', 'REJECTED',
                'EXPIRED', 'UNKNOWN', 'QUARANTINED'
            )
        )
    ) then
        raise exception 'invalid OMS status transition'
            using errcode = '55000';
    end if;
    return new;
end;
$function$;

create or replace function
    capital_cipher.guard_execution_command_transition()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    if tg_op = 'DELETE' then
        raise exception 'execution commands cannot be deleted'
            using errcode = '55000';
    end if;
    if old.command_id is distinct from new.command_id
       or old.oms_order_id is distinct from new.oms_order_id
       or old.command_type is distinct from new.command_type
       or old.max_attempts is distinct from new.max_attempts
       or old.available_at is distinct from new.available_at
       or old.created_at is distinct from new.created_at
       or not (
            (old.status = 'PENDING' and new.status = 'LEASED')
            or (
                old.status = 'LEASED'
                and new.status in ('LEASED', 'COMPLETED', 'DEAD_LETTER')
            )
       ) then
        raise exception 'invalid execution command transition'
            using errcode = '55000';
    end if;
    return new;
end;
$function$;

revoke all on function
    capital_cipher.reject_oms_evidence_mutation()
from public;
revoke all on function
    capital_cipher.guard_oms_order_transition()
from public;
revoke all on function
    capital_cipher.guard_execution_command_transition()
from public;

create trigger trg_oms_order_events_immutable
before update or delete on capital_cipher.oms_order_events
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_execution_fills_immutable
before update or delete on capital_cipher.execution_fills
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_reconciliation_runs_immutable
before update or delete on capital_cipher.reconciliation_runs
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_reconciliation_mismatches_immutable
before update or delete on capital_cipher.reconciliation_mismatches
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_venue_position_snapshots_immutable
before update or delete on capital_cipher.venue_position_snapshots
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_venue_balance_snapshots_immutable
before update or delete on capital_cipher.venue_balance_snapshots
for each row execute function capital_cipher.reject_oms_evidence_mutation();
create trigger trg_oms_orders_transition
before update or delete on capital_cipher.oms_orders
for each row execute function capital_cipher.guard_oms_order_transition();
create trigger trg_execution_commands_transition
before update or delete on capital_cipher.execution_commands
for each row execute function
    capital_cipher.guard_execution_command_transition();

alter table capital_cipher.oms_orders enable row level security;
alter table capital_cipher.oms_order_events enable row level security;
alter table capital_cipher.execution_commands enable row level security;
alter table capital_cipher.execution_fills enable row level security;
alter table capital_cipher.reconciliation_runs enable row level security;
alter table capital_cipher.reconciliation_mismatches enable row level security;
alter table capital_cipher.venue_position_snapshots enable row level security;
alter table capital_cipher.venue_balance_snapshots enable row level security;

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

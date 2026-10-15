-- Capture immutable Bybit Futures TESTNET protection in the same transaction
-- that creates the OMS order. The application runtime can read this evidence
-- but cannot write or mutate it directly.

set lock_timeout = '5s';
set statement_timeout = '60s';

create table capital_cipher.oms_order_protections (
    oms_order_id varchar(36) primary key
        references capital_cipher.oms_orders (oms_order_id)
        on delete restrict,
    approval_id varchar(64) not null unique
        references capital_cipher.order_approvals (approval_id)
        on delete restrict,
    exchange varchar(16) not null,
    environment varchar(16) not null,
    symbol text not null,
    side varchar(4) not null,
    execution_intent varchar(16) not null,
    reduce_only boolean not null,
    stop_loss numeric(38, 18) not null,
    take_profit numeric(38, 18) not null,
    leverage numeric(20, 8) not null,
    reference_price numeric(38, 18) not null,
    created_at timestamptz not null default now(),
    constraint ck_oms_protection_exchange
        check (exchange = 'BYBIT'),
    constraint ck_oms_protection_environment
        check (environment = 'TESTNET'),
    constraint ck_oms_protection_side
        check (side in ('BUY', 'SELL')),
    constraint ck_oms_protection_intent
        check (execution_intent in ('ENTRY', 'EXIT')),
    constraint ck_oms_protection_entry_reduce_only
        check (
            (execution_intent = 'ENTRY' and reduce_only = false)
            or (execution_intent = 'EXIT' and reduce_only = true)
        ),
    constraint ck_oms_protection_prices
        check (
            stop_loss > 0
            and take_profit > 0
            and reference_price > 0
        ),
    constraint ck_oms_protection_leverage
        check (leverage >= 1),
    constraint ck_oms_protection_brackets_entry
        check (
            execution_intent <> 'ENTRY'
            or (
                side = 'BUY'
                and stop_loss < reference_price
                and reference_price < take_profit
            )
            or (
                side = 'SELL'
                and take_profit < reference_price
                and reference_price < stop_loss
            )
        )
);

revoke all on table capital_cipher.oms_order_protections from public;
alter table capital_cipher.oms_order_protections enable row level security;

drop policy if exists oms_order_protections_runtime_select
    on capital_cipher.oms_order_protections;
create policy oms_order_protections_runtime_select
    on capital_cipher.oms_order_protections
    for select
    to capital_cipher_runtime
    using (true);

grant select on table capital_cipher.oms_order_protections
    to capital_cipher_runtime;

create or replace function capital_cipher.capture_bybit_order_protection()
returns trigger
language plpgsql
security definer
set search_path = ''
as $function$
declare
    risk_payload jsonb;
    captured_stop_loss numeric(38, 18);
    captured_take_profit numeric(38, 18);
    captured_leverage numeric(20, 8);
begin
    if new.exchange <> 'BYBIT' or new.environment <> 'TESTNET' then
        return new;
    end if;

    select evaluation.payload
      into risk_payload
      from capital_cipher.order_approvals approval
      join capital_cipher.risk_evaluations evaluation
        on evaluation.evaluation_id = approval.evaluation_id
     where approval.approval_id = new.approval_id
       and approval.risk_check_id = new.risk_check_id
       and evaluation.approved = true;

    if risk_payload is null then
        raise exception 'Bybit TESTNET order has no approved durable risk evidence'
            using errcode = '55000';
    end if;

    captured_stop_loss := nullif(risk_payload ->> 'stop_loss', '')::numeric;
    captured_take_profit := nullif(risk_payload ->> 'take_profit', '')::numeric;
    captured_leverage := nullif(risk_payload ->> 'leverage', '')::numeric;

    if captured_stop_loss is null
       or captured_take_profit is null
       or captured_leverage is null then
        raise exception 'Bybit TESTNET entry requires durable SL/TP/leverage'
            using errcode = '55000';
    end if;

    if captured_leverage is distinct from new.leverage then
        raise exception 'Bybit TESTNET leverage differs from approved risk evidence'
            using errcode = '55000';
    end if;

    if new.side = 'BUY'
       and not (
           captured_stop_loss < new.reference_price
           and new.reference_price < captured_take_profit
       ) then
        raise exception 'Bybit BUY protection does not bracket reference price'
            using errcode = '55000';
    end if;

    if new.side = 'SELL'
       and not (
           captured_take_profit < new.reference_price
           and new.reference_price < captured_stop_loss
       ) then
        raise exception 'Bybit SELL protection does not bracket reference price'
            using errcode = '55000';
    end if;

    insert into capital_cipher.oms_order_protections (
        oms_order_id,
        approval_id,
        exchange,
        environment,
        symbol,
        side,
        execution_intent,
        reduce_only,
        stop_loss,
        take_profit,
        leverage,
        reference_price,
        created_at
    ) values (
        new.oms_order_id,
        new.approval_id,
        new.exchange,
        new.environment,
        new.symbol,
        new.side,
        'ENTRY',
        false,
        captured_stop_loss,
        captured_take_profit,
        captured_leverage,
        new.reference_price,
        now()
    );

    return new;
end;
$function$;

revoke all on function capital_cipher.capture_bybit_order_protection()
    from public;

drop trigger if exists trg_capture_bybit_order_protection
    on capital_cipher.oms_orders;
create trigger trg_capture_bybit_order_protection
after insert on capital_cipher.oms_orders
for each row execute function capital_cipher.capture_bybit_order_protection();

create or replace function capital_cipher.reject_oms_order_protection_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $function$
begin
    raise exception 'OMS order protection evidence is immutable'
        using errcode = '55000';
end;
$function$;

revoke all on function capital_cipher.reject_oms_order_protection_mutation()
    from public;

create trigger trg_oms_order_protections_immutable
before update or delete on capital_cipher.oms_order_protections
for each row execute function capital_cipher.reject_oms_order_protection_mutation();

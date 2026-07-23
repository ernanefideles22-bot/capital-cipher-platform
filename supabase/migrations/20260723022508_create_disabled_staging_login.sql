-- Create the hosted PAPER application login in a deliberately disabled state.
-- A strong password is assigned out-of-band only when all provider secrets are
-- ready. Until then PASSWORD NULL makes remote authentication impossible.

do $block$
begin
    if not exists (
        select 1 from pg_roles where rolname = 'capital_cipher_staging'
    ) then
        execute
            'create role capital_cipher_staging '
            'login password null connection limit 8 '
            'nosuperuser nocreatedb nocreaterole noreplication nobypassrls';
    end if;

    if exists (
        select 1
        from pg_roles
        where rolname = 'capital_cipher_staging'
          and (
              not rolcanlogin
              or rolsuper
              or rolcreatedb
              or rolcreaterole
              or rolreplication
              or rolbypassrls
              or rolconnlimit <> 8
          )
    ) then
        raise exception
            'capital_cipher_staging violates the hosted PAPER role boundary'
            using errcode = '42501';
    end if;
end;
$block$;

grant capital_cipher_runtime to capital_cipher_staging;

alter role capital_cipher_staging set search_path = capital_cipher, public;
alter role capital_cipher_staging set row_security = on;
alter role capital_cipher_staging set statement_timeout = '30s';
alter role capital_cipher_staging set lock_timeout = '5s';
alter role capital_cipher_staging
    set idle_in_transaction_session_timeout = '60s';

comment on role capital_cipher_staging is
    'Disabled-by-default LOGIN for Capital Cipher hosted PAPER staging';

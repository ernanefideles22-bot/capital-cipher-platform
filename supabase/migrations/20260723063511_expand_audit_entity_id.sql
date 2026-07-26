-- Audit entities include content-addressed SHA-256 identifiers. Preserve the
-- complete identifier instead of constraining every entity to UUID length.

set lock_timeout = '5s';
set statement_timeout = '60s';

alter table capital_cipher.audit_logs
    alter column entity_id type text;

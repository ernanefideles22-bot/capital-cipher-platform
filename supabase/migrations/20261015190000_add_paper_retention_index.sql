-- Support bounded PAPER retention without changing runtime DELETE privileges.
-- The maintenance worker still archives rows before deletion and uses the
-- migration/admin connection; the application role remains append-only.

set lock_timeout = '5s';
set statement_timeout = '60s';

create index if not exists ix_agent_execution_jobs_retention
    on capital_cipher.agent_execution_jobs (completed_at, execution_id)
    where status in ('COMPLETED', 'DEAD_LETTER')
      and completed_at is not null;

comment on index capital_cipher.ix_agent_execution_jobs_retention is
    'Supports bounded PAPER-only archival retention scans of terminal jobs.';

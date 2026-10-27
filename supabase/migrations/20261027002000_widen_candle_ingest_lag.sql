-- Preserve every historical value while admitting >24.8-day backfill delays.
-- Apply transactionally, after reviewed backup, free-space and downtime checks.
-- int4 -> int8 may rewrite this table and requires ACCESS EXCLUSIVE.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $migration$
DECLARE
    current_type regtype;
BEGIN
    SELECT atttypid::regtype INTO current_type
    FROM pg_attribute
    WHERE attrelid = 'capital_cipher.candle_observations'::regclass
      AND attname = 'ingest_lag_ms' AND NOT attisdropped;

    IF current_type = 'integer'::regtype THEN
        ALTER TABLE capital_cipher.candle_observations
            ALTER COLUMN ingest_lag_ms TYPE bigint USING ingest_lag_ms::bigint;
    ELSIF current_type IS DISTINCT FROM 'bigint'::regtype THEN
        RAISE EXCEPTION 'INGEST_LAG_UNEXPECTED_COLUMN_TYPE';
    END IF;
END
$migration$;

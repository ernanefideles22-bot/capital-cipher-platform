-- Widen existing venue checks without deleting or rewriting historical rows.
-- Apply transactionally after backup and staging review.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE capital_cipher.oms_orders
    DROP CONSTRAINT ck_oms_order_exchange,
    ADD CONSTRAINT ck_oms_order_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));
ALTER TABLE capital_cipher.execution_fills
    DROP CONSTRAINT ck_execution_fill_exchange,
    ADD CONSTRAINT ck_execution_fill_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));
ALTER TABLE capital_cipher.reconciliation_runs
    DROP CONSTRAINT ck_reconciliation_run_exchange,
    ADD CONSTRAINT ck_reconciliation_run_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));
ALTER TABLE capital_cipher.reconciliation_mismatches
    DROP CONSTRAINT ck_reconciliation_mismatch_exchange,
    ADD CONSTRAINT ck_reconciliation_mismatch_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));
ALTER TABLE capital_cipher.venue_position_snapshots
    DROP CONSTRAINT ck_venue_position_exchange,
    ADD CONSTRAINT ck_venue_position_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));
ALTER TABLE capital_cipher.venue_balance_snapshots
    DROP CONSTRAINT ck_venue_balance_exchange,
    ADD CONSTRAINT ck_venue_balance_exchange CHECK (exchange IN ('BINANCE', 'BYBIT', 'GATE'));

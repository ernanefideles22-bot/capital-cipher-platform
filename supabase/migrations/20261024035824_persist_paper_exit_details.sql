-- Additive recovery: preserve original order fields and immutable audit records.
ALTER TABLE capital_cipher.paper_orders
    ADD COLUMN IF NOT EXISTS exit_price numeric,
    ADD COLUMN IF NOT EXISTS exit_reason text;

-- Only recover facts recorded for this exact closed order and correlation.
-- Missing or malformed evidence remains NULL instead of fabricating history.
WITH closure_evidence AS (
    SELECT DISTINCT ON (a.entity_id, a.correlation_id)
        a.entity_id,
        a.correlation_id,
        CASE WHEN jsonb_typeof(a.payload::jsonb -> 'exit_price') = 'number'
             THEN (a.payload ->> 'exit_price')::numeric END AS exit_price,
        CASE WHEN jsonb_typeof(a.payload::jsonb -> 'exit_reason') = 'string'
             THEN NULLIF(a.payload ->> 'exit_reason', '') END AS exit_reason
    FROM capital_cipher.audit_logs a
    WHERE a.audit_type = 'PAPER_ORDER_CLOSED'
      AND a.entity_type = 'paper_order'
      AND a.payload ->> 'paper_order_id' = a.entity_id
      AND a.payload ->> 'correlation_id' = a.correlation_id
      AND a.payload ->> 'status' = 'CLOSED'
    ORDER BY a.entity_id, a.correlation_id, a.created_at DESC, a.id DESC
)
UPDATE capital_cipher.paper_orders p
SET exit_price = COALESCE(p.exit_price, CASE WHEN e.exit_price > 0 THEN e.exit_price END),
    exit_reason = COALESCE(p.exit_reason, e.exit_reason)
FROM closure_evidence e
WHERE p.id = e.entity_id
  AND p.correlation_id = e.correlation_id
  AND p.status = 'CLOSED'
  AND (p.exit_price IS NULL OR p.exit_reason IS NULL);

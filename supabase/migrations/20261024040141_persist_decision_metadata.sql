ALTER TABLE capital_cipher.decisions
    ADD COLUMN IF NOT EXISTS strategy text,
    ADD COLUMN IF NOT EXISTS warnings jsonb;

-- Older decisions already have an immutable full candidate in the audit trail.
WITH candidate_evidence AS (
    SELECT DISTINCT ON (a.entity_id, a.correlation_id)
        a.entity_id, a.correlation_id,
        CASE WHEN jsonb_typeof(a.payload::jsonb -> 'strategy') = 'string'
             THEN NULLIF(a.payload ->> 'strategy', '') END AS strategy,
        CASE WHEN jsonb_typeof(a.payload::jsonb -> 'warnings') = 'array'
             THEN a.payload::jsonb -> 'warnings' END AS warnings
    FROM capital_cipher.audit_logs a
    WHERE a.audit_type = 'DECISION_CANDIDATE'
      AND a.entity_type = 'decision'
      AND a.payload ->> 'decision_id' = a.entity_id
      AND a.payload ->> 'correlation_id' = a.correlation_id
    ORDER BY a.entity_id, a.correlation_id, a.created_at DESC, a.id DESC
)
UPDATE capital_cipher.decisions d
SET strategy = COALESCE(d.strategy, e.strategy),
    warnings = COALESCE(d.warnings, e.warnings)
FROM candidate_evidence e
WHERE d.id = e.entity_id AND d.correlation_id = e.correlation_id
  AND (d.strategy IS NULL OR d.warnings IS NULL);

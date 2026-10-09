-- Telemetry only: preserve sub-millisecond execution durations.
ALTER TABLE capital_cipher.agent_outputs
    ALTER COLUMN latency_ms TYPE double precision USING latency_ms::double precision;

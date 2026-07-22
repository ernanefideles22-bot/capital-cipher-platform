# Month 4 — durable immutable experiment artifacts

This is the third Month 4 increment. Walk-forward reports can now be persisted
as content-addressed, append-only research artifacts in PostgreSQL or SQLite.
The operational PAPER path remains unchanged.

No schema change was applied to a hosted Supabase project by this work.

## Storage boundary

The new table lives in the internal service schema:

```text
capital_cipher.walk_forward_experiments
```

It is not exposed through a browser-facing Supabase Data API. The existing
database bootstrap revokes `PUBLIC` access to the internal schema and its
tables. This increment also revokes `PUBLIC` access to internal sequences and
the immutability trigger function.

The table uses:

- a PostgreSQL `bigint GENERATED ALWAYS AS IDENTITY` technical primary key;
- a unique public `experiment_id`;
- a unique SHA-256 `artifact_hash`;
- a versioned `walk-forward-artifact-v1` metadata envelope;
- queryable dataset, candidate, protocol, symbol, timeframe, status, and
  timestamp columns;
- the complete report as JSONB on PostgreSQL and JSON on SQLite;
- composite indexes for candidate/time and dataset/time query shapes;
- database checks that hashes have the expected length and promotion status is
  always `RESEARCH_ONLY`.

No JSONB index is created because current reads use structured columns or the
unique experiment identity, not arbitrary payload predicates.

## Artifact identity

`experiment_id` still identifies the dataset, protocol, candidate, execution
assumptions, and simulation context. `artifact_hash` independently protects
the deterministic report content.

The artifact hash includes folds, segment dataset identities, validation/test
results, aggregates, risk/strategy context, and execution assumptions. It
excludes only:

```text
created_at
duration_ms
artifact_hash
```

Those are runtime metadata or the hash itself. Repeating an identical
experiment therefore resolves to the same artifact even if wall-clock
duration differs.

## Atomic idempotency

Persistence uses one transaction and:

```text
INSERT ... ON CONFLICT (experiment_id) DO NOTHING
```

If the identity already exists, the stored payload and checksum are validated:

- identical content returns the original stored report;
- different deterministic content under the same identity fails closed;
- corrupted or inconsistent stored fields fail integrity validation.

Before recomputing folds, the engine looks up the deterministic experiment
identity. A completed artifact is returned directly, which avoids duplicate
work and makes repeated API requests idempotent.

## Database-level mutation guards

PostgreSQL installs a trigger that rejects both `UPDATE` and `DELETE` on the
artifact table. SQLite installs equivalent update and delete triggers for
local development and tests.

The application exposes insert, load, and bounded list operations only. It
does not expose update or delete methods. Administrative data-retention or
legal-erasure procedures would require an explicit, separately reviewed
operation that disables or replaces these guards.

This is database-enforced append-only storage, not an external WORM archive.
Database owners and infrastructure administrators still retain ultimate
control over the database and backups.

## API behavior

With a configured repository:

```text
POST /api/v1/backtest/walk-forward
GET  /api/v1/backtest/walk-forward/reports
GET  /api/v1/backtest/walk-forward/reports/{experiment_id}
```

read and write durable artifacts. Clearing the process-local report cache does
not remove them. Without a repository, local isolated runs preserve the prior
in-memory behavior.

The full report continues to carry:

```text
promotion_status = RESEARCH_ONLY
```

Persistence cannot enable a strategy, alter risk limits, change system mode,
or authorize real execution.

## Deployment boundary and Month 4 completion

The artifact schema and trigger are represented by a reviewed, versioned
Supabase migration and exercised against disposable PostgreSQL in CI. No
migration has been applied to a hosted project by this work.

Versioned train-only fitting, pre-registered acceptance gates, minimum sample
sizes, multiple-testing correction, search budgets, historical
spread/funding data, and margin/liquidation mechanics are completed in
[`month-4-completion.md`](month-4-completion.md).

External WORM archival and organization-specific retention policy remain
future governance hardening, not part of the Month 4 backtester scope.

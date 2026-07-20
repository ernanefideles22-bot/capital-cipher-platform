# Month 3 — durable backfill queue and raw data lake

This increment moves historical imports out of the HTTP request lifecycle.
The protected API validates and atomically submits work; a background worker
claims it with a renewable ownership boundary, runs the trusted-clock
workflow, and acknowledges, retries, or dead-letters the item.

## Operator API

```text
POST /api/v1/market/backfills
GET  /api/v1/market/backfills/{job_id}
GET  /api/v1/market/backfills/{job_id}/lineage
```

`POST` returns a `PENDING` job and queue item without waiting for provider
pagination. All three endpoints require `X-API-Key`. The lineage response
joins the request, queue state, raw provider pages, immutable raw-object
metadata, normalized dataset manifest, and any gaps attributed to the job.

## Durable claim protocol

Queue state is stored in `capital_cipher.backfill_queue_items`.

```text
PENDING / RETRY / expired LEASED
  → SELECT ... FOR UPDATE SKIP LOCKED
  → LEASED (owner, expiry, incremented attempt)
  → COMPLETED
     or RETRY (bounded exponential delay)
     or DEAD_LETTER
```

PostgreSQL workers skip rows already locked by another worker. A process crash
does not lose the item: another worker may reclaim it after lease expiry.
Expired work at the configured attempt limit is failed and dead-lettered
without another provider call. Submitting the same normalized request is
idempotent; an operator may deliberately resubmit a dead-lettered request,
which resets only its queue-attempt budget while preserving the job's complete
execution history.

The lease defaults to one hour because a bounded 100,000-candle import can
require many public REST pages. Provider rate-budget coordination and lease
heartbeats remain future hardening before horizontal production scale.

## Raw-first data path

Every successful public REST page is captured before provider-specific parsing
and before normalized candles are persisted:

```text
provider JSON page
  → canonical UTF-8 JSON
  → SHA-256
  → deterministic gzip object
  → immutable object metadata + job/page lineage transaction
  → normalize and validate candles
  → time-series rows
  → dataset manifest
```

Objects use paths such as:

```text
lake://raw/binance.public-rest/2026/07/20/ab/{sha256}.json.gz
```

The local adapter writes through a unique temporary file, flushes it, and
atomically replaces the final content-addressed path. Repeating the same page
does not rewrite its object or duplicate its lineage edge. A hash check is
performed when an object is read. If object storage or lineage persistence
fails, normalization does not advance.

The current adapter is a private service filesystem mounted as a Docker
volume. Its interface is intentionally storage-neutral. A future Supabase
deployment should use a **private Storage bucket** through the Storage API (or
an S3-compatible private bucket), keep object metadata in this internal
PostgreSQL schema, and never mutate the Supabase `storage` schema directly.
No hosted Supabase project or policy is changed by this increment.

## Internal tables

```text
capital_cipher.backfill_queue_items
capital_cipher.raw_data_objects
capital_cipher.backfill_raw_pages
```

The existing database bootstrap keeps the `capital_cipher` schema inaccessible
to `PUBLIC`; browser roles receive no direct grants. Object bytes are not
served by an unauthenticated endpoint.

## Configuration

```text
BACKFILL_WORKER_ENABLED=true
BACKFILL_WORKER_POLL_INTERVAL_SECONDS=1
BACKFILL_LEASE_SECONDS=3600
BACKFILL_MAX_ATTEMPTS=5
BACKFILL_RETRY_BASE_SECONDS=5
BACKFILL_RETRY_MAX_SECONDS=300
DATA_LAKE_ROOT=.capital-cipher-data-lake
```

Disable the worker only for maintenance or deterministic API tests. Queue and
job state remain durable while it is disabled.

## Verification

Local tests cover idempotent submission, exclusive lease ownership, expired
lease recovery, bounded dead-lettering, retries, raw-object hash verification,
raw-before-normalized failure semantics, and lineage API authorization. CI
also starts PostgreSQL 16 and proves two concurrent claims receive different
rows through the production dialect.

# Month 3 — trusted clocks, continuity, and historical backfills

## Outcome

This increment turns the first time-series warehouse into a repairable data
pipeline:

- Binance and Bybit public server-time endpoints are probed with an NTP-style
  midpoint estimator;
- every clock observation is persisted before it becomes trusted runtime
  evidence;
- normalized streaming ingestion fails closed when clock evidence is missing,
  stale, or `UNSAFE`;
- gaps are detected during streaming ingestion and by explicit bounded scans;
- public historical REST clients can repair Binance spot and Bybit linear
  candle ranges;
- every backfill is idempotent, audited, quality-checked, and linked to a
  deterministic dataset manifest.

No private exchange key, account endpoint, order endpoint, testnet execution,
or live execution capability is present.

## Public provider boundaries

The adapters use only:

```text
Binance  GET /api/v3/time
Binance  GET /api/v3/klines
Bybit    GET /v5/market/time
Bybit    GET /v5/market/kline?category=linear
```

Binance public market data defaults to
`https://data-api.binance.vision`. Bybit defaults to
`https://api.bybit.com`. Both base URLs are operator-only environment
configuration and are never accepted from an API request.

Provider payloads are validated and normalized into the existing Candle v1
contract. Binance pages forward from the requested open timestamp; Bybit pages
backward because its v5 response is reverse ordered. The final internal batch
is always chronological and duplicate-free.

## Trusted clock gate

Clock evidence has four runtime outcomes:

```text
SYNCED   accepted
WARNING  accepted with a data-quality penalty and CLOCK_WARNING
UNSAFE   blocked
UNKNOWN  blocked (missing, stale, or future-dated evidence)
```

Defaults:

```text
warning offset                    500 ms
unsafe offset                    2000 ms
warning round trip              1000 ms
unsafe round trip               5000 ms
probe interval                    30 s
maximum observation age           90 s
```

When `ENABLE_MARKET_DATA=1`, the backend probes Binance before starting
normalized ingestion and runs a recurring monitor for Binance and Bybit. Raw
public WebSocket payloads can still be retained for audit, but no normalized
candle reaches the trusted warehouse, agents, risk, or paper engine without
acceptable clock evidence.

The gate is enabled by default with
`REQUIRE_TRUSTED_MARKET_CLOCK=true`.

## Gap model

`capital_cipher.market_data_gaps` records a deterministic SHA-256 identity for:

```text
(exchange, symbol, timeframe, missing start, missing end)
```

Each gap has an explicit missing-candle count and status:

```text
OPEN → FILLING → RESOLVED
                 FAILED
```

Streaming ingestion automatically scans the interval between the last trusted
candle and the new candle. Operators can also request a bounded scan:

```text
POST /api/v1/market/gaps/scan
GET  /api/v1/market/gaps
```

Both endpoints require the administrator key. A repeat scan upserts the same
gap identity and resolves previously open gaps that are no longer missing.

## Historical backfill workflow

Protected endpoint:

```text
POST /api/v1/market/backfills
GET  /api/v1/market/backfills/{job_id}
```

The requested range is inclusive and expressed in normalized candle close
timestamps. Its end must not exceed the exchange clock, so an open or future
candle cannot enter a historical dataset. The workflow is:

```text
validate bounded range
  → persist RUNNING job
  → probe and persist exchange clock
  → enforce trusted clock gate
  → fetch public historical pages
  → validate one ordered series
  → evaluate data quality
  → batch insert idempotently
  → rescan and persist remaining gaps
  → materialize deterministic dataset manifest
  → persist terminal job
```

The job ID is the SHA-256 fingerprint of the normalized request. Repeating a
completed request returns the prior result without another provider call.
Partial and failed jobs can be retried under the same identity and increment
their attempt count.

Terminal states:

```text
COMPLETED  every expected candle is present
PARTIAL    provider returned data but gaps remain
BLOCKED    clock evidence is unsafe
FAILED     provider, validation, quality, or persistence failure
```

The job records retrieved and inserted counts, remaining gaps, clock
observation ID, clock status, dataset hash, attempts, and sanitized error
details.

## Storage and security

New tables remain in the non-exposed internal schema:

```text
capital_cipher.market_data_gaps
capital_cipher.historical_backfill_jobs
```

The existing schema creation path revokes `PUBLIC` access. No grants are made
to `anon`, `authenticated`, or browser clients. No hosted Supabase project is
modified by this increment.

## Configuration

```text
REQUIRE_TRUSTED_MARKET_CLOCK
CLOCK_PROBE_INTERVAL_SECONDS
CLOCK_OBSERVATION_MAX_AGE_SECONDS
CLOCK_WARNING_OFFSET_MS
CLOCK_UNSAFE_OFFSET_MS
CLOCK_WARNING_ROUND_TRIP_MS
CLOCK_UNSAFE_ROUND_TRIP_MS
HISTORICAL_BACKFILL_MAX_CANDLES
PUBLIC_MARKET_HTTP_TIMEOUT_SECONDS
BINANCE_PUBLIC_REST_URL
BYBIT_PUBLIC_REST_URL
```

The global import ceiling defaults to 100,000 candles. Provider requests are
paged in batches of at most 1,000 records.

## Deferred work

Durable queued backfills, distributed job leases, bounded retries, raw REST
payload archiving, and lineage are delivered by the next Month 3 increment;
see `month-3-durable-data-lake.md`. Provider rate-budget coordination, lease
heartbeats, bulk object-storage imports, and native PostgreSQL partitioning
remain deferred until measured volume justifies them.

# Month 3 — time-series and data catalog foundation

## Outcome

This increment creates the first reproducible market-data warehouse:

- normalized candles are persisted before agents, risk, or paper trading;
- logical candle identity excludes ingestion time but includes every market
  fact, so the same source data receives the same SHA-256 identity;
- exact duplicates are ignored and conflicting corrections fail closed;
- ordered candle selections receive immutable, versioned dataset manifests;
- every backtest report records the dataset ID and content hash it used;
- NTP-style clock probes classify a source as `SYNCED`, `WARNING`, or
  `UNSAFE`.

## Storage boundary

New warehouse tables live in the PostgreSQL `capital_cipher` schema:

```text
capital_cipher.candle_observations
capital_cipher.dataset_manifests
capital_cipher.clock_observations
```

The backend creates this schema only over a trusted direct database connection,
revokes `PUBLIC` access, and never exposes it through the frontend. SQLite maps
the schema to its default namespace for isolated tests.

This is deliberate for Supabase: the warehouse is an internal service store,
not a browser-facing Data API. No grants are made to `anon`, `authenticated`,
or `service_role`.

## Time-series model

`candle_observations` uses:

- SHA-256 logical candle ID as primary key;
- unique `(exchange, symbol, timeframe, closed_at)` identity;
- exact `NUMERIC(38,18)` OHLCV storage;
- database checks for positive prices, OHLC invariants, non-negative volume,
  and quality score bounds;
- composite `(exchange, symbol, timeframe, closed_at)` index;
- quality status, warnings, errors, received time, and ingest lag.

Batch inserts use one transaction and idempotent conflict handling. A different
OHLCV value for an existing series timestamp is not silently overwritten.

## Dataset manifests

A manifest contains:

- candle contract version;
- exchange, symbol, timeframe, and inclusive time range;
- deterministic row order and row count;
- SHA-256 hash over ordered logical candle IDs;
- data-quality summary;
- clock-quality status at materialization.

The manifest ID is `candles:v1:<dataset_hash>`. Re-loading the same CSV with a
different local ingestion time produces the same ID. Changing any market fact
changes it.

Protected endpoints:

```text
POST /api/v1/market/datasets
GET  /api/v1/market/datasets/{dataset_hash}
```

Backtests with `source=store` now read the persistent warehouse instead of the
bounded in-memory window.

## Clock quality

`evaluate_clock_probe` compares the exchange/server time with the midpoint of
the local request and response. It records signed offset and round-trip time.
Default gates are:

```text
warning offset:       500 ms
unsafe offset:       2000 ms
warning round trip:  1000 ms
unsafe round trip:   5000 ms
```

Connecting these probes to Binance and Bybit server-time endpoints is the next
increment. An `UNSAFE` result will then block trusted ingestion.

## Partitioning decision

The first table is intentionally unpartitioned. Native monthly PostgreSQL
partitions add operational complexity and are introduced only after measured
growth or retention needs justify them. The time-first composite key and query
shape are already compatible with a later range-partition migration.

The legacy `public.market_candles` table is left untouched. No automatic
backfill is attempted because it lacks the full ingestion and quality metadata
required by the new identity contract.

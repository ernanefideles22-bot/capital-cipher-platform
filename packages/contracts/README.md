# Capital Cipher contracts

This package is the language-neutral boundary between data ingestion, the
event bus, agents, APIs, and storage.

- `manifest.json` declares the active contract version.
- `schemas/v1/` contains immutable JSON Schema contracts.
- Breaking changes require a new major directory such as `schemas/v2/`.
- Additive, backward-compatible changes require a manifest version bump and
  compatibility tests in every consumer.

The Python backend remains the first consumer. Future agent runtimes and
services must validate messages at their boundaries rather than importing
backend implementation classes.

The v1 family currently includes event, candle, raw source, replay checkpoint,
dataset manifest, clock observation, market-data gap, historical backfill job,
durable backfill queue, and content-addressed raw object contracts.
Backtest execution assumptions are also versioned so cost-model changes cannot
silently alter experiment results.
The walk-forward protocol fixes train/validation/test sizing, embargo, step,
anchoring, and fold limits for reproducible research evaluations.
Walk-forward artifact metadata is versioned separately so future runtimes can
verify storage identity, checksums, lineage, and research-only status.
Historical spread/funding observations, isolated-margin assumptions,
pre-registered research budgets, and acceptance gates complete the Month 4
research boundary without authorizing operational execution.
Agent inputs, outputs, registrations, execution requests, durable jobs, and
complete traces form the Month 5 PAPER-only runtime boundary. These contracts
do not expose risk mutation or order execution authority.

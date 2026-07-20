# Month 2 — contracts and event foundation

## Scope of this increment

This increment formalizes the existing repository as a polyglot monorepo
without relocating the working backend or dashboard.

1. `packages/contracts` is the language-neutral source for boundary schemas.
2. Every event topic and payload carries an immutable major contract version.
3. The in-process event bus journals an event before delivering it.
4. The deduplication window is bounded so memory use cannot grow forever.
5. Public exchange payloads are stored before normalization and analysis.
6. Raw payload identity is deterministic and includes a SHA-256 integrity
   checksum.

## Data path

```text
Public exchange WebSocket
  -> raw-market-event v1
  -> raw_market_events table
  -> event_journal table
  -> normalization
  -> data quality
  -> PAPER agents and decisions
```

If raw persistence or event journaling fails, downstream delivery stops. This
is deliberate: a decision must never depend on market input that cannot be
replayed or audited.

## Compatibility rules

- `schemas/v1` is immutable for breaking changes.
- Additive changes require consumer compatibility tests.
- Breaking changes create `schemas/v2` and a documented migration window.
- Backend models must pass the published JSON Schema contract tests.
- Topics end with their major version, such as `market.events.v1`.

## Explicit non-goals

- No private exchange API.
- No order-management system.
- No live or testnet execution.
- No external broker selection yet.
- No claim of exactly-once delivery across processes.

The Redis Streams adapter and deterministic replay checkpoints are documented
in [`month-2-redis-replay.md`](month-2-redis-replay.md). PAPER mode remains
mandatory.

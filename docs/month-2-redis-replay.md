# Month 2 — Redis Streams and deterministic replay

## Decision

Redis Streams is the first external event transport. The domain continues to
depend on the broker-neutral `EventTransport` boundary, so Redis can be
replaced without changing producers.

Delivery is explicitly **at least once**. Exactly-once delivery is not claimed.
Consumers must use `event_id` as their idempotency key.

## Publish path

```text
validate payload for secrets
  -> append immutable event_journal row
  -> publish to Redis Stream
  -> mark event_outbox row as published
  -> deliver to in-process PAPER consumers
```

If Redis fails in optional mode, local PAPER delivery continues and the event
remains pending in `event_outbox`. A background dispatcher retries it. If Redis
is configured as required, local delivery stops until publication succeeds.

The outbox is a separate additive table. Existing `event_journal` deployments
do not require destructive alteration.

## Replay checkpoints

Every candle dataset receives a SHA-256 fingerprint. After a consumer handles
an event successfully, the replay stores:

- dataset hash;
- next offset;
- last event hash;
- processed count;
- RUNNING, FAILED, or COMPLETED status.

A restarted replay resumes from the next confirmed offset. If the dataset
changes or the last-event hash does not match, replay fails closed.

There is a narrow crash window after a handler succeeds but before its
checkpoint commits. That event may be delivered again. This is intentional
at-least-once behavior, and downstream consumers must remain idempotent.

## Runtime modes

- No `REDIS_URL`: in-process bus only.
- `REDIS_URL` with optional broker: outbox retry plus PAPER continuity.
- `EVENT_BROKER_REQUIRED=1`: Redis failure blocks downstream delivery.

Redis is internal-only in Docker Compose and uses AOF persistence. The CI runs
an actual Redis 8.2 service for the transport integration test.

The direct publisher and outbox dispatcher share a process-local lock to avoid
racing on the same pending event. Before the backend is horizontally scaled,
the dispatcher still needs database-backed claiming or leasing so multiple
instances cannot publish the same row concurrently. Duplicate-safe consumers
remain mandatory either way.

## Security

Keys named as tokens, passwords, authorization headers, API keys, API secrets,
private keys, or refresh/access tokens are rejected before journal storage.
The broker has a configurable message-size limit and bounded stream retention.

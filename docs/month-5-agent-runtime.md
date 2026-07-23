# Month 5 — governed PAPER agent runtime complete

Month 5 is complete for the roadmap scope: a version-aware registry,
language-neutral input/output contracts, memory isolated by execution, a
durable leased queue, bounded retries, complete traces, and an initial cohort
of exactly 15 analytical agents in PAPER mode.

This milestone adds analytical capacity only. It does not add exchange
credentials, an order adapter, live/testnet modes, risk-limit mutation, or
execution authority. The three existing primary agents retain the exact
decision authority they had before Month 5. The 12 new agents are shadow
observers whose outputs are recorded but cannot change operational direction,
confidence, warnings, risk, or paper orders.

## Completion evidence

| Requirement | Implementation |
| --- | --- |
| Registry | Ordered, version-aware `AgentRegistry` with explicit registration, lifecycle state, definition hashes, replacement preconditions, and history |
| Input/output contracts | Six immutable JSON Schema v1 boundaries, validated against the Python runtime in tests |
| Per-execution memory | Append-only `INPUT`, `ATTEMPT`, and terminal `OUTPUT`/`DEAD_LETTER` entries scoped by `execution_id` and protected by payload hashes |
| Queue | Database-backed PAPER-only jobs with idempotency keys, availability times, leases, partial ready/expired indexes, and dead letters |
| Retries | Exponential, bounded retries with configured attempt and delay ceilings |
| Traceability | Job, ordered attempts, ordered memory, correlation ID, agent version, definition hash, worker identity, events, and terminal output |
| Initial cohort | Exactly 15 agents: 3 primary and 12 shadow |
| Persistence | SQLAlchemy models for SQLite/PostgreSQL plus a versioned Supabase migration in the private `capital_cipher` schema |
| Security | Protected execution APIs, secret-pattern rejection, PAPER database check, RLS, public privilege revocation, and immutable evidence triggers |
| Recovery | Expired leases can be reclaimed; stale workers cannot commit after losing lease ownership |

## Runtime architecture

```text
Validated AgentExecutionRequestV1
  -> secret-pattern gate
  -> versioned AgentRegistry lookup
  -> deterministic request fingerprint + idempotency check
  -> durable PENDING job + isolated INPUT memory
  -> short transaction lease claim
  -> bounded-concurrency agent execution
  -> append-only attempt + attempt memory
       -> RETRY with bounded backoff
       -> COMPLETED + OUTPUT memory
       -> DEAD_LETTER + failure memory
  -> complete AgentExecutionTraceV1
```

The runtime has two consumption paths over the same repository contract:

- inline execution, used by the orchestrator when a candle closes;
- a background worker, used by the protected enqueue API.

Both paths use the same leases, retries, idempotency, evidence, and terminal
state rules. The in-memory repository is contract-equivalent for isolated
tests and backtests, but only the SQL repository is durable across process
restarts.

## Fixed Month 5 cohort

| Agent | Role | Capability |
| --- | --- | --- |
| `MarketDataAgent` | PRIMARY | Market availability and candle freshness |
| `QuantAgent` | PRIMARY | RSI, MACD, volume, and quantitative direction |
| `TrendAgent` | PRIMARY | Trend and moving-average regime |
| `MomentumAgent` | SHADOW | Rate of change and RSI momentum |
| `VolatilityAgent` | SHADOW | ATR and realized-volatility class |
| `VolumeAgent` | SHADOW | Relative volume and participation |
| `VWAPAgent` | SHADOW | Rolling VWAP displacement |
| `MACDAgent` | SHADOW | MACD direction and histogram |
| `EMAAlignmentAgent` | SHADOW | 9/21/50 EMA alignment |
| `MeanReversionAgent` | SHADOW | Rolling close-price z-score |
| `BreakoutAgent` | SHADOW | Previous-range breakout |
| `SupportResistanceAgent` | SHADOW | Range support/resistance proximity |
| `CandleStructureAgent` | SHADOW | Body and wick structure |
| `LiquidityProxyAgent` | SHADOW | Explicitly labelled OHLCV liquidity proxy |
| `DataQualityAgent` | SHADOW | Ordering, duplicate, and OHLCV checks |

All 12 new specialists are deterministic and read only from the normalized
`CandleStore`. They receive no `RiskManager`, `PaperTradingEngine`, exchange
adapter, credential, or database mutation capability.

The decision engine records all 15 outputs in `agent_summary`, but only
`MarketDataAgent`, `QuantAgent`, and `TrendAgent` are operational inputs. A
regression test proves that an opposing, timed-out shadow output with warnings
does not change the action, confidence, or warnings of the primary decision.

## Versioned boundaries

The following schemas are published under `packages/contracts/schemas/v1/`:

- `agent-input.schema.json`
- `agent-output.schema.json`
- `agent-registration.schema.json`
- `agent-execution-request.schema.json`
- `agent-execution-job.schema.json`
- `agent-execution-trace.schema.json`

Every registration includes the registry version, agent version, required
inputs, capabilities, input/output contract names, execution mode, decision
role, timeout, maximum attempts, enabled state, and a canonical SHA-256
definition hash.

A queued job captures both the requested agent version and definition hash.
If the declared definition changes without a version bump before execution,
or the version is no longer active, the job fails closed into a terminal dead
letter. Replacement is explicit, requires the expected active version,
disables the old instance, and preserves its registration in registry
history.

## Idempotency, leases, and retries

The request fingerprint is a canonical SHA-256 of the validated request. The
job identity is that fingerprint, while the unique idempotency boundary is:

```text
(agent_name, agent_version, idempotency_key)
```

Submitting the same key and request returns the original job. Reusing the key
with a different request fails closed.

Workers claim only ready `PENDING`/`RETRY` jobs or expired leases. PostgreSQL
uses `FOR UPDATE SKIP LOCKED` inside a short transaction so workers do not
serialize behind the same ready row. An expired lease reuses the same attempt
number: analytical agents may be evaluated again after a worker crash, but
they are read-only and idempotent, and only the current lease owner can commit
evidence.

Retries use bounded exponential delay:

```text
delay = min(AGENT_RETRY_MAX_SECONDS,
            AGENT_RETRY_BASE_SECONDS * 2^(attempt - 1))
```

`FAILED` and `TIMEOUT` may retry until `AGENT_MAX_ATTEMPTS`. Contract,
registration, version, or definition mismatches do not retry.

## Memory and trace evidence

Memory is deliberately execution-scoped rather than shared globally. Each
entry has a monotonically unique sequence within its execution and a canonical
payload hash. The trace contains:

- the current durable job;
- every completed attempt in order;
- every execution memory entry in order;
- the final structured output when terminal.

Attempt and memory rows are append-only. SQLite and PostgreSQL both reject
updates and deletes with database triggers. Job state remains mutable only for
the queue state machine and is protected by lease-owner and attempt-order
checks in the repository transaction.

Agent exceptions are reduced to their exception type before logging or
persistence. Requests also pass the platform secret-pattern scanner before a
job is created. Runtime tests verify that an exception message containing a
sentinel secret never enters serialized trace evidence.

## API and operations

The read-only registry and health endpoints remain:

```text
GET /api/v1/agents
GET /api/v1/agents/status
```

Durable execution endpoints require `X-API-Key` matching `ADMIN_API_KEY`:

```text
POST /api/v1/agents/executions
GET  /api/v1/agents/executions?limit=100
GET  /api/v1/agents/executions/{execution_id}
```

The POST endpoint enqueues only; it does not execute in the request process.
The background worker starts after the state machine reaches PAPER and stops
cleanly during application shutdown.

Runtime configuration:

```text
AGENT_TIMEOUT_MS=5000
AGENT_MAX_ATTEMPTS=3
AGENT_MAX_CONCURRENCY=8
AGENT_WORKER_ENABLED=1
AGENT_WORKER_POLL_INTERVAL_SECONDS=0.25
AGENT_WORKER_BATCH_SIZE=4
AGENT_LEASE_SECONDS=30
AGENT_RETRY_BASE_SECONDS=0.05
AGENT_RETRY_MAX_SECONDS=0.2
```

Validation constrains timeouts, concurrency, attempts, leases, polling, and
retry delays. Inconsistent retry bounds prevent application startup.

## PostgreSQL and Supabase lifecycle

The versioned migration is:

```text
supabase/migrations/20260720074545_create_agent_runtime.sql
```

It creates three private tables, queue indexes, foreign-key indexes, database
checks, RLS defense in depth, public privilege revocations, and a
`SECURITY INVOKER` append-only trigger. CI applies every tracked migration to
a disposable PostgreSQL 16 database and verifies the runtime trace, row-level
security, triggers, and function security.

No hosted Supabase project is linked or modified by this milestone.

## Completion boundary

Month 5 proves that the platform can safely host and trace the first 15
specialized PAPER agents through a governed runtime. It does not prove signal
quality, profitability, or scale to 300 concurrent analytical workloads.
Agent evaluation, marginal contribution, portfolio construction, central risk,
OMS, exchange testnet adapters, load testing, and shadow-trading duration
remain assigned to later roadmap months.

The Month 5 exit gate is therefore:

1. exactly 15 registered PAPER agents;
2. all six v1 contracts validated against a real runtime trace;
3. durable queue, idempotency, lease recovery, retry, timeout, and dead-letter
   tests passing;
4. isolated append-only memory and PostgreSQL/SQLite mutation rejection
   passing;
5. protected execution API and shadow non-interference passing;
6. the complete repository quality gate and PostgreSQL CI passing.

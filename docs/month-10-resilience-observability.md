# Month 10 — resilience, observability and cost control

Month 10 expands the governed runtime from 150 to exactly 200 PAPER agents
and introduces a small operational control plane. It measures the platform,
evaluates explicit service-level objectives, preserves alert and resilience
evidence, controls shadow-work cost, and fails safely when a critical
dependency is unavailable.

This boundary is intentionally observational. It cannot submit or cancel an
order, consume exchange credentials, approve risk, bypass the OMS, or select a
LIVE environment. LIVE execution remains absent from the platform.

## Acceptance boundary

The month is complete only when all of these invariants pass:

- exactly 200 enabled PAPER agents: 3 PRIMARY and 197 SHADOW;
- 50 new deterministic, read-only OHLCV diagnostics;
- bounded metric memory and correlation-aware snapshots;
- four explicit SLO evaluations with error-budget evidence;
- append-only OPENED/RESOLVED alert lifecycle;
- attributed daily cost accounting and fail-safe shadow admission;
- critical dependency loss produces `SAFE_HALT`;
- optional dependency loss produces `DEGRADED`;
- critical recovery requires three consecutive healthy confirmations;
- deterministic load and chaos harnesses pass without an execution adapter;
- private PostgreSQL storage has RLS, revocation and mutation guards;
- administrative read/evaluation APIs require `X-API-Key`;
- no HTTP endpoint can inject faults;
- all 49 v1 contracts and the complete monorepo quality gate pass.

## 200-agent cohort

The 50 new Month 10 agents read normalized OHLCV only:

| Family | Count | Diagnostic |
|---|---:|---|
| Return entropy | 6 | directional uncertainty |
| Jump intensity | 6 | large-return frequency |
| Volume anomaly | 6 | recent/reference participation shift |
| Gap pressure | 6 | open-to-previous-close pressure |
| Trend persistence | 6 | signed return persistence |
| Volatility of volatility | 5 | instability of realized movement |
| Close location | 5 | close position inside the candle range |
| Wick imbalance | 5 | lower-versus-upper rejection |
| Downside capture | 5 | downside-versus-upside squared returns |

Every definition is versioned, deterministic and `SHADOW`. Evidence explicitly
states `read_only: true`, `decision_authority: false`, and
`order_authority: false`. Capabilities do not contain order submission,
credentials or risk approval.

## Operational metrics

`BoundedMetricRegistry` owns counters, gauges and bounded histograms. Each
histogram keeps at most `OPERATIONS_METRIC_CAPACITY` samples, preventing
unbounded process memory. A snapshot contains:

- the correlation identity and configured observation window;
- registered and active agent counts;
- counters for executions, failures, timeouts and orchestrator cycles;
- p95, average and maximum agent/orchestrator latency;
- current cost utilization;
- dependency health gauges.

Snapshot identities are immutable. Correlation IDs connect an operational
observation to the decision or administrative cycle that produced it.

## SLOs and error budgets

The control plane evaluates four objectives:

| SLO | Default target | Comparator |
|---|---:|---|
| Agent execution success | 99% | greater than or equal |
| Agent p95 latency | 2,000 ms | less than or equal |
| Orchestrator cycle success | 99% | greater than or equal |
| Orchestrator p95 latency | 5,000 ms | less than or equal |

No observations produce `NO_DATA`; they never produce invented compliance.
Measured objectives produce `HEALTHY`, `WARNING` or `BREACHED`. Each
evaluation carries its target, measurement, sample count, comparator and
remaining error-budget percentage. A breach opens one alert. Repeated
evaluations do not duplicate an active alert. Recovery appends a separate
`RESOLVED` event rather than mutating history.

## Cost guard

Every non-empty agent batch records quantity, unit cost, estimated USD cost,
cost center, resource and correlation identity. The daily budget has three
states:

- `HEALTHY`: normal PAPER scheduling;
- `WARNING`: normal scheduling with visible budget pressure;
- `HARD_LIMIT`: suspend all SHADOW admission.

The hard limit never disables PRIMARY admission, central risk, audit, or the
kill switch. It reduces optional work; it does not weaken a safety boundary.
The default unit cost is zero until an operator supplies an evidence-based
estimate.

## Dependency and recovery matrix

| Dependency | Class | Failure behavior | Recovery |
|---|---|---|---|
| Database | Critical | `SAFE_HALT`; no decision evaluation | 3 healthy probes |
| Audit | Critical | `SAFE_HALT`; no decision evaluation | 3 healthy probes |
| Central risk | Critical | `SAFE_HALT`; no decision evaluation | 3 healthy probes |
| Broker | Optional | `DEGRADED`; PRIMARY only | next healthy probe |
| Market data | Optional | `DEGRADED`; PRIMARY only | next healthy probe |
| Shadow runtime | Optional | `DEGRADED`; PRIMARY only | next healthy probe |

A critical failure latches the halt. All critical dependencies must each
accumulate the configured number of consecutive healthy confirmations before
the latch clears. A single new failure resets that dependency's confirmation
counter.

## Load, chaos and recovery evidence

`backend/scripts/run_month10_resilience.py` is a deterministic,
credential-free acceptance harness. It:

1. creates the normal registry in memory;
2. seeds synthetic normalized candles;
3. initializes all 200 agents;
4. executes every agent through the versioned runtime;
5. asserts PAPER-only mode, exactly three PRIMARY agents, no order capability,
   bounded duration, bounded p95 latency and zero runtime errors;
6. injects a local critical database-health failure into an isolated recovery
   coordinator and proves `SAFE_HALT` plus conservative recovery;
7. injects an isolated optional broker-health failure and proves degraded
   PRIMARY-only behavior.

The harness never constructs exchange credentials or an order adapter. Every
`ResilienceTestRun` contains `live_execution_attempted: false`, measured
latency/throughput/error rate and named invariant results. Fault injection is
available only in this local/CI harness, never over HTTP.

Run it from `backend`:

```bash
python scripts/run_month10_resilience.py
```

## Storage and Supabase boundary

Migration
`20260720201528_create_operational_observability_resilience.sql` adds five
tables to the private `capital_cipher` schema:

- `operational_metric_snapshots`;
- `slo_evaluations`;
- `operational_alert_events`;
- `cost_usage_records`;
- `resilience_test_runs`.

All are append-only. PostgreSQL uses a `SECURITY INVOKER` mutation-rejection
function and per-table triggers. RLS is enabled. Access is revoked from
`public`, `anon` and `authenticated`, so these tables are not browser/Data API
surfaces. SQLite installs equivalent UPDATE and DELETE rejection triggers for
local and CI evidence.

This branch creates the migration file only. Applying it to a hosted Supabase
project remains a separate, explicitly reviewed action.

## Protected API

All operational endpoints require the configured administrative key:

```text
GET  /api/v1/operations/status
GET  /api/v1/operations/metrics
POST /api/v1/operations/slos/evaluate
GET  /api/v1/operations/slos
GET  /api/v1/operations/alerts
GET  /api/v1/operations/costs
GET  /api/v1/operations/resilience-runs
```

The metrics endpoint creates a non-persistent point-in-time snapshot. Manual
SLO evaluation is audited. List endpoints return bounded evidence. There is no
mutation endpoint for alerts, costs or resilience evidence and no
chaos-injection API.

## Runbooks

### Critical dependency alert

1. Confirm the alert source and correlation identity.
2. Keep the system in `SAFE_HALT`; do not disable the gate.
3. Repair the dependency outside the trading path.
4. Observe three consecutive healthy probes for database, audit and risk.
5. Confirm the OPENED and RESOLVED lifecycle events are both persisted.
6. Resume PAPER observation only; TESTNET promotion requires its own review.

### Optional dependency alert

1. Confirm `DEGRADED` mode and PRIMARY-only admission.
2. Verify central risk, audit and kill switch remain healthy.
3. Repair the optional dependency.
4. Confirm a healthy probe restores the normal 200-agent cohort.

### Cost hard limit

1. Confirm the attributed records and daily time boundary.
2. Keep PRIMARY, risk and audit enabled.
3. Leave SHADOW work suspended while investigating unexpected usage.
4. Adjust the budget or unit cost only through reviewed configuration.
5. Never treat a higher budget as permission for LIVE execution.

### SLO breach

1. Inspect measurement, target, sample count and error budget.
2. Use the correlation ID to reconstruct affected cycles.
3. Check dependency alerts and recent resilience runs.
4. Reproduce locally with the PAPER harness.
5. Resolve the cause; do not manually rewrite alert history.

## Configuration

Month 10 settings are documented in `backend/.env.example`:

- monitor enable flag and interval;
- metric sample capacity and SLO window;
- daily budget, warning threshold and unit execution cost;
- agent/orchestrator success and latency objectives;
- required consecutive recovery confirmations.

Defaults are conservative and PAPER-only. Configuration validation rejects
unbounded capacities, invalid budgets, impossible SLO ranges and unsafe
recovery confirmation counts.

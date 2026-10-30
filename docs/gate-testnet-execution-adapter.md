# Gate Testnet execution adapter

`GateTestnetExecutionAdapter` implements the execution-adapter interface for Gate
USDT perpetual futures. It is deliberately not registered in `build_context` and
does not change the deployed read-only service. Constructor and transport write
switches default to off. All HTTP requests use the fixed Testnet origin;
production hosts cannot be configured.

## Implemented behavior

- Prepare deterministic approval-derived client IDs and integer contract sizing
  for BTCUSDT, ETHUSDT and SOLUSDT. Preserve Gate's tick and current mark price.
- Require the immutable PostgreSQL protection record, exact prepared OMS size,
  Gate TESTNET scope, fresh metadata, approved price/notional bounds, a flat
  account, single position mode, isolated 1x and sufficient USDT margin.
- Persist client identity and intent before every dispatch. The SQLite journal
  serializes account cycles across connections; in-process operations share an
  async lock so reconciliation does not race an in-flight entry.
- Confirm entry with separate order and position reads. A partial IOC protects
  the actual filled contracts. A proven zero-fill IOC returns CANCELED and
  releases its slot only after full flat-account evidence.
- Confirm the owned stop with a fresh independent lookup and position read.
  Provider acknowledgements are never treated as stop confirmation.
- If protection fails after a proven entry, re-read entry ownership and position,
  persist one emergency close intent and attempt one reduce-only close. A failed
  or successful emergency close ends HALTED; it never resets an earlier halt,
  guesses an unknown stop ID or permits another entry.
- `close_protected_order(original_entry)` proves scope, ownership and protection,
  persists a distinct exit ID, verifies the reduce-only close independently,
  cancels only the owned stop and requires a fresh flat account inventory before
  CLOSED. An uncertain outcome is never automatically retried.
- An OMS cancel command for a terminal IOC returns its terminal snapshot; it
  does not cancel the stop or close the position.
- Reconcile orders, fills, positions, balance and protection. Inventory scans
  follow signed pagination, detect duplicate IDs and fail closed at 2,000 rows.
  Equity includes unrealized PnL. Contract quantities are converted to base
  units. Negative fees (rebates) fail closed because the shared OMS fill schema
  does not represent them; they are never silently changed into trading costs.
- Preserve verified terminal entry snapshots for closed cycles, with original
  observation times, so exchange history retention does not destroy local
  reconciliation. Active positions and stops always require current reads.

## Runtime integration contract

The caller owns the journal, PostgreSQL protection store, metadata provider and
an exclusive account lease. Use one durable journal per dedicated Testnet account,
shared by all writers; do not create a fresh journal to bypass unresolved work.
The async `account_guard` must check central risk/kill controls and guarantee that
this lease remains held throughout each call, including protective cleanup. A
boolean callback alone is not a distributed lease implementation.

`close_protected_order` is an explicit protected-cycle operation, not a generic
OMS SUBMIT command. The runtime must persist its exit receipt and fills in its
position/accounting lifecycle before connecting automated strategy exits. Exit
fills remain visible with their genuine exchange identities; they are not
mislabelled as entry fills.

A stop that triggers, expires, disappears or changes ends protection verification
and requires reconciliation. This implementation does not automatically resume a
halted cycle, renew expiring stops or infer an executed stop from a flat position.
A continuous strategy runner needs that explicit stop-terminal/accounting path
before activation. Stop expiration defaults to one hour in the existing planner.

No runtime registration, environment activation, halt reset, canary exchange
order or Railway deployment is included in this change. Offline tests do not
certify exchange behavior, strategy profitability or readiness for real funds.

## Validation

From `backend/`:

```sh
python -m pytest app/tests/test_gate*.py app/tests/test_month_7_oms.py app/tests/test_demo_execution.py -q
```

Local result on 2026-10-30: 362 passed, 2 skipped (POSTGRES_TEST_URL not
configured), and 6 subtests passed. The PostgreSQL migrations were not changed.

Execution tests include an entire entry/stop/exit cycle through the actual
transport using `httpx.MockTransport`, checking HMAC signatures and raw payloads.
They also exercise partial fills, zero fills, wrong ownership/scope, emergency
close, uncertainty, task cancellation, restart, risk revocation, historical
retention, inventory saturation and read/write serialization. No exchange is
contacted and no credentials are read by these tests.

API field references: [Gate perpetual-futures API](https://www.gate.com/docs/developers/apiv4/en/futures/),
sections for contracts, accounts, orders, personal trades and price-triggered orders.

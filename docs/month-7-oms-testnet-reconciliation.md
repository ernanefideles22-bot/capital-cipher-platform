# Month 7 — OMS, exchange TESTNET and reconciliation

## Outcome

Month 7 introduces one internal order boundary for every approved decision.
PAPER remains the default. TESTNET is opt-in and supports only Binance Spot
Test Network or Bybit V5 linear Testnet. Bybit Spot is deliberately outside
the Month 7 safety boundary. There is no LIVE environment, live host fallback,
public submission endpoint, or frontend-to-exchange path.

The completed flow is:

```text
Decision
  -> central risk approval
  -> OMS durable identity
  -> PAPER atomic mirror
     or TESTNET transactional command
  -> isolated exchange adapter
  -> immutable transition evidence
  -> continuous venue reconciliation
  -> durable positions/balances/fills
  -> central risk portfolio refresh
  -> kill switch on critical drift
```

## Safety boundary

- `SYSTEM_MODE` remains restricted to `OFFLINE|PAPER`.
- `OMS_EXECUTION_ENVIRONMENT` accepts only `PAPER|TESTNET`.
- TESTNET requires `OMS_TESTNET_ENABLED=1`.
- TESTNET also requires
  `OMS_TESTNET_ACKNOWLEDGEMENT=TESTNET_ONLY_NO_REAL_FUNDS`.
- TESTNET boot requires PostgreSQL with the Month 7 migration.
- Binance execution accepts only `https://testnet.binance.vision`.
- Bybit execution accepts only `https://api-testnet.bybit.com`.
- `BYBIT_TESTNET_CATEGORY` accepts only `linear`.
- Credentials are read directly from process environment by an ephemeral
  provider. They are not Pydantic settings and are never persisted, audited,
  published or returned by an API.
- `POST /api/v1/oms/orders` does not exist. Approved orders originate only in
  the orchestrator after the central risk manager mints a single-use approval.
- Every TESTNET command has `max_attempts=1`. An ambiguous timeout or HTTP 5xx
  becomes `UNKNOWN`; reconciliation determines the venue result. Writes are
  never blindly retried.

These protections apply to sandbox trading only. They are not evidence that a
strategy is profitable or suitable for real capital.

## Durable OMS

`oms_orders` holds immutable order identity and versioned current state.
`oms_order_events` is append-only evidence with one event per state version.
The database rejects identity mutation, version jumps, terminal-state mutation
and invalid lifecycle transitions.

PAPER creation consumes the approval, inserts the simulator order, inserts the
OMS mirror and appends the first OMS event in one transaction. TESTNET creation
consumes the approval, inserts the OMS order and inserts the `SUBMIT` command in
one short transaction. External HTTP is performed only after commit.
Pending, submitted, partially filled, cancel-pending and unknown commands
reserve their remaining notional in the central portfolio before another
approval can be consumed.

`execution_commands` is a transactional outbox. Workers claim rows using a
bounded lease and PostgreSQL `FOR UPDATE SKIP LOCKED`. A command records a
single external attempt, then becomes immutable completion evidence.

## Exchange adapters

Both adapters implement the same fail-closed interface:

- healthcheck;
- submit order;
- cancel order;
- fetch venue orders, fills, positions and balances;
- close transport.

Binance uses signed Spot Test Network requests, bounded `recvWindow` and
client-generated order identity. Bybit uses the V5 linear signing format,
queries active and historical order evidence, and treats create/cancel
responses as asynchronous acknowledgements. Both rely on `client_order_id`
for later correlation. Before approval consumption, each adapter reads its
public instrument rules and rounds quantity down with decimal arithmetic;
minimum quantity, maximum quantity and minimum notional are enforced before
the durable command exists.

The adapters contain no retry loop for writes. Read pagination is bounded.
The application creates only the adapter selected by configuration.

## Reconciliation

Each run compares local OMS identity with venue state and persists:

- order status and cumulative fill corrections;
- idempotent fills;
- position snapshots;
- balance snapshots;
- typed mismatches;
- run counts, timing, status and error type.

Critical mismatch categories include an OMS order missing at the venue, an
unmanaged venue order or fill, terminal-state contradiction, position
quantity drift, and adapter unavailability. By default any critical mismatch
activates the durable central kill switch. Bybit one-way positions are
reconciled on signed net quantity. Bybit position snapshots and Binance Spot
base-asset balances from the latest successful run are restored into central
risk exposure on restart.

## Database and Supabase

Migration:

`supabase/migrations/20260720143646_create_oms_testnet_reconciliation.sql`

It adds eight private `capital_cipher` tables, indexes every foreign-key access
path, uses partial indexes for active orders and claimable commands, enables
RLS, revokes `public`, `anon` and `authenticated`, and installs
`SECURITY INVOKER` mutation guards. No hosted Supabase project is changed by
this branch.

Apply migrations through the normal reviewed deployment workflow before
enabling TESTNET. Do not point TESTNET at the default SQLite development file.

## Internal API

Read views:

```text
GET /api/v1/oms/status
GET /api/v1/oms/orders
GET /api/v1/oms/orders/{oms_order_id}
GET /api/v1/oms/reconciliation/latest
```

Authenticated controls:

```text
POST /api/v1/oms/orders/{oms_order_id}/cancel
POST /api/v1/oms/reconciliation/run
```

Cancellation only queues a durable TESTNET command. It does not call an
exchange from the request transaction. Cancellation is accepted only after
venue acknowledgement (or for an ambiguous `UNKNOWN` order), which prevents a
cancel transition from racing an uncommitted submission result. If the kill
switch is active before dispatch, queued submissions are quarantined without a
venue call; already acknowledged cancellations remain allowed.

## Configuration

Safe default:

```dotenv
SYSTEM_MODE=PAPER
OMS_EXECUTION_ENVIRONMENT=PAPER
OMS_TESTNET_ENABLED=0
```

Intentional TESTNET example:

```dotenv
SYSTEM_MODE=PAPER
OMS_EXECUTION_ENVIRONMENT=TESTNET
OMS_TESTNET_ENABLED=1
OMS_TESTNET_ACKNOWLEDGEMENT=TESTNET_ONLY_NO_REAL_FUNDS
OMS_TESTNET_EXCHANGE=BINANCE
DATABASE_URL=postgresql+asyncpg://...
CAPITAL_CIPHER_BINANCE_TESTNET_KEY_ID=...
CAPITAL_CIPHER_BINANCE_TESTNET_SIGNING_SECRET=...
```

Never commit credential values. Bybit uses the equivalent
`CAPITAL_CIPHER_BYBIT_TESTNET_*` variables.

## Completion evidence

The Month 7 suite covers:

- exact testnet URL allowlists and configuration gates;
- credential redaction;
- Binance and Bybit signatures;
- venue quantity-step and minimum-notional normalization;
- ambiguous-write handling with no retry;
- atomic PAPER mirror;
- durable TESTNET queue, lease and state history;
- immediate portfolio reservation for in-flight TESTNET orders;
- reconciliation correction, fill and balance persistence;
- active/historical venue discovery and signed-net position reconciliation;
- critical orphan-order/orphan-fill detection and durable kill switch;
- JSON Schema publication;
- private RLS migration and absence of a submission API.

The full local backend suite must pass before publication. PostgreSQL/RLS and
Redis integration remain mandatory CI gates.

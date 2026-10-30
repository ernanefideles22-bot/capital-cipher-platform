# Dedicated Gate Testnet OMS connection

The Gate status service can now compose the existing `VenueAwareRiskManager`,
`OMSService`, PostgreSQL repository and `ReconciliationService` with the protected
Gate adapter. The main backend remains on its PAPER configuration. No generic
staging safety validator is weakened and no public order-execution route exists.

The initial execution scope is BTCUSDT / 15m, isolated 1x, one active cycle,
central risk capped at 0.1% and at most 50 USDT notional. The 300-agent research
cohort supplies a candidate; its advisory risk result is discarded. The runtime
independently refreshes Gate candles and mark, runs central risk again, atomically
consumes its approval, and queues the durable OMS command. WAIT/BLOCK and synthetic
canary decisions cannot open positions. A stable candle identity prevents repeat
approvals after restart.

The account lease is a PostgreSQL advisory lock keyed by the actual Gate user ID,
not by an API key. A second process using another key for the same account cannot
obtain the lock. The existing mounted cycle journal is bound to that account.
Lost leases stop dispatch. OMS workers claim and expire only commands for their
exact exchange and environment.

An open cycle is monitored separately from slow agent analysis. The runtime
checks the owned stop and position, closes reduce-only at the central target,
and closes before stop expiry without canceling protection first. If the stop
triggers, closure requires its exact generated-order linkage (`trade_id`), a
filled reduce-only close and fresh flat-account inventory. A flat position alone
never releases the slot. Missing or failed stop evidence remains blocked.

Exit fills retain their genuine venue order IDs and direction and are linked to
the original managed OMS cycle through durable entry/exit identities. A successful
flat Gate reconciliation supersedes historical entry exposure in central risk.
Daily starting equity and all-time equity peak survive process restart in the
existing durable journal. The runtime does not reset central kill controls.

## Configuration and gates

Existing service and volume:

- `capital-cipher-gate-testnet` in `staging`.
- Existing `/gate-data/capital-cipher-gate-testnet/cycles.sqlite3`.
- Existing Gate Testnet key/secret variables; never copy them into source or logs.
- `DATABASE_URL`: PostgreSQL runtime-role connection, which can reference the
  existing backend variable through Railway's reference syntax.
- `GATE_OMS_CONNECTION_ENABLED=true` composes the connection; default is off.
- `GATE_OMS_WRITES_ENABLED=false` connects without sending orders. Default is off.
- Enabling writes additionally requires
  `GATE_OMS_ACKNOWLEDGEMENT=GATE_TESTNET_VIRTUAL_FUNDS_ONLY`.

The write flag is **not** release authorization. Every entry and dispatch also
requires an exact-revision release, clear durable central risk, a valid account
lease and reconciled account state. By default this is the existing external
audit `APPROVED_TESTNET` decision. The separately configured
[owner-controlled Gate demo policy](gate-demo-owner-release.md) supports an
explicit one-order canary and requires acceptance of measured evidence before
agent mode; it never creates an independent attestation. No release evidence, independent
attestation, canary result or approval is fabricated by this runtime.

A requested connection that cannot initialize causes `/health` to return 503.
The public status distinguishes connection, flow, revision authorization, risk
clearance and actual execution enablement, without revealing credentials or
account balances. A disabled write flag never closes or modifies existing
positions. A read-only connection failure does not alter the PAPER kill switch.

## Validation and activation

Tests exercise actual central risk, the actual durable OMS and repository with a
simulated Gate protocol: queue, entry, stop, target exit, opposite-direction exit
fills, flat-risk release, duplicate signals, WAIT, expired authorization, lease
isolation and persistent equity baselines. A PostgreSQL integration test checks
that two sessions cannot own the same account lease. Local runs skip that test
when `POSTGRES_TEST_URL` is absent; CI supplies PostgreSQL.

Do not equate CI success with a verified exchange trade. Connect in read-only
mode first. Autonomous writes remain blocked until the exact deployed revision
has the required release and accepted Gate Testnet entry/stop/exit evidence.
Owner grants and external audit attestations remain distinct authorization bases. Preserve current journals and any unresolved
halt; never replace a journal to make the status clear.

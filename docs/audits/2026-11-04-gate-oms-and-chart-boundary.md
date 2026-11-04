# Gate OMS isolation and public chart boundary

Based on deployed Gate revision `767f9e20d59154b435426876b54ecc4aa5fc758f`.
The authoritative venue remains Gate.io USDT futures testnet.

## Changes

Scope OMS initialization, listing and ID lookup to the configured venue and
environment before deserializing database rows. Reject foreign cached results
and commands. A foreign order ID cannot be used to queue a cancel. PAPER workers
do not lease external commands. The existing database queue filtering, including
expired-lease recovery, remains scoped to Gate/TESTNET and is tested against a
foreign pending/expired command matrix.

Add a GET-only public `/api/gate/market/candles` route for BTCUSDT, ETHUSDT and
SOLUSDT at 15m, 1h and 4h. It reuses the existing Gate testnet public candle
adapter, requests at most 200 bars, returns explicit venue/environment identity,
and caches each of the nine allowed market/frame combinations for 30 seconds.
Concurrent same-key reads share a lock. Failures are sanitized; there is no
cross-venue or stale fallback. HTTP responses remain no-store for clients.

The chart route has no account credential dependency, no access to S3 capture or
journals, no OMS operation, and does not start research or modify monitors.
The existing status response also identifies the running source revision.

## Verification

Local focused OMS/service/market tests: 69 passed, 1 skipped. S3 collector,
service integration, paired evidence, durable journal, equity valuation, Gate
execution simulator, release and durable-risk tests: 179 passed. These are offline
tests with mock transports or disposable databases, not broker operations or
financial acceptance. Full-head CI results are recorded on the PR.

One initial local subprocess run used the other checkout's editable Python
package and failed import compatibility. Re-running with this checkout's
explicit PYTHONPATH passed the full affected selection. No production code or
threshold was changed to mask that environment error.

## Rollout boundary

The primary dashboard proposal is in `fix/gate-platform-alignment-20261104`,
stacked on backend OMS hotfix #104. Each runtime needs its own scoped code;
publishing the dashboard alone does not patch the separate Gate service.

No release, owner grant, strategy, risk, S3 policy/freeze, journal, schema,
database or volume is changed here. No real orders are sent by these tests.
No deployment or merge is included. An eventual authorized Gate deployment
changes the runtime revision and restarts the operational stability window;
continue the durable financial journal only under its unchanged policy and
integrity checks. Do not represent old release grants or cycle counts as
approval of the new revision.

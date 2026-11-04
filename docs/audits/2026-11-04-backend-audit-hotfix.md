# Backend audit corrections — 2026-11-04

Base: deployed backend revision `9ab29dccd48d41b53cce0e286e019bb0ff75223b`
on `feat/binance-demo-full-integration-20261028`. This patch does not include
the separate Gate or strategy research branches.

## OMS storage boundaries

The older backend could deserialize every OMS record in the shared database,
including `GATE`, which its Exchange enum does not support. Initialize, list
and get now filter by the service's configured exchange AND environment in
SQL, before mapping records. List limits apply after those predicates.
Foreign IDs behave as not found, including cancellation lookups.

Command leasing and expired-lease recovery use the same order scope. A PAPER
worker never leases external execution commands. No schema, enum, adapter,
historical record or execution permission is changed. Repository scope remains
optional for existing internal callers; the OMS service always supplies it.

## PAPER cost units and compatibility

Basic PAPER stores `slippage_estimated` as a quote **price delta**, not a cash
cost. Performance now estimates its monetary impact as:

`stored_price_delta * (position_size / filled_entry_price)`

The estimate uses the original rounded stored values. For example, a stored
delta of 0.02 with quantity 66.653336 is about 1.333067 in quote currency,
not 0.02. Legacy order fields and the original fills are preserved.

The additive `cost_reporting` metadata identifies units and model. Order list
and detail responses retain original `orders`/`order` objects and add parallel
derived cost reports. The performance response includes the same metadata.

- `BASIC_ENTRY_ONLY_V1`: adverse slippage at entry only; exit slippage is not
  modeled. This correction does not make historical execution realistic.
- `REALISTIC_LEDGER_V1`: reports existing ledger cash costs; volume impact is
  already included in slippage, so it is not added again to execution totals.
  Per-order slippage reports explicitly identify that they include spread.
- Both aggregate only closed trades, as before. Orders can also expose estimates
  for their currently accrued costs.
- Gross PnL already uses filled prices. Net PnL already incorporates that price
  impact and fees. The reporting correction does not subtract slippage again,
  rewrite historical PnL or change balance, risk limits, entries or exits.
- Realistic ledgers are not persisted; durable recovery remains unsupported and
  fails closed. A missing per-order ledger is reported as unavailable.

## Release evidence

When declared `PASSED` belongs to another revision, `/status` reports
`REVISION_MISMATCH`; missing revision provenance yields `REVISION_UNVERIFIED`.
`declared_status`, bundle identity and original evidence revision are retained.
An existing failure is not overwritten by a provenance warning. Matching
revisions alone never turn a failed or unset declaration into `PASSED`.

This is evidence presentation, not a new homologation or execution approval.

## Validation and deployment boundary

Regression coverage in `test_audit_backend_regressions.py` reproduces a raw Gate
row alongside Binance PAPER, Binance TESTNET and Bybit TESTNET. An expired Gate
lease must remain untouched while a Binance worker claims its own command.
The SQLite fixture relaxes its legacy CHECK only during disposable fixture
insertion to represent the already-expanded shared schema; production
constraints are not relaxed.

PAPER tests check BUY/SELL cash conversion, unchanged serialized trades and PnL,
repeat recovery, API metadata and realistic impact inclusion. Release tests
cover matching, differing and missing revisions and preserve negative states.

Run from `backend`:

```sh
python -m pytest -q app/tests/test_audit_backend_regressions.py
python -m pytest -q
```

Initial local results (Python 3.12), before the guard correction below:
12 new regression cases passed; complete backend
suite: 728 passed, 34 skipped, 1 failed in 180.62 seconds. Dashboard production
build and TypeScript compilation passed. `git diff --check` passed.

The initial failure was `test_security.py::test_no_real_order_code_in_backend`.
The original test rejected the literal `/fapi/v1/order` in the existing DEMO tests
(the DEMO implementation also contains it). Re-running the security file in a
detached, unmodified checkout of base `9ab29dc` reproduces exactly the failure:
5 passed, 1 failed. Initial CI run 41832492021 independently reproduced the
same scanner failure: 760 passed, 2 skipped, 1 failed.

### Follow-up: security guard reflects the actual application boundary

The guard now excludes negative fixtures only in the actual `app/tests`
subtree and permits the relative Futures order path only in the two existing
isolated modules, `demo/venue.py` and `demo/execution.py`. Absolute endpoint
prohibitions remain active in production modules, including DEMO. Live Futures
origins are explicitly forbidden inside DEMO. An AST check rejects absolute or
relative imports of DEMO by the main backend. Mutation cases verify these
restrictions, including a production file disguised with a `test_` filename.

`test_demo_network_boundary.py` independently checks the literal HTTPS Demo
origin for every allowlisted request, rejects hostile hosts/paths/methods before
transport, requires credentials and synchronized clock for signed requests, and
checks that GET/POST/DELETE redirects (301/302/303/307/308) never forward to live.
All network requests terminate in `httpx.MockTransport`; no exchange is contacted.

The selected security, DEMO, and audit regression suites pass: **133 tests**.
Full revalidation is tracked in PR #104 and its commit-specific CI. Production
DEMO code, execution flags, routing, credentials and permissions are unchanged.
Local integration skips require the CI job with disposable PostgreSQL/Redis;
a green test suite is not a runtime deployment or a new release-readiness bundle.

After review, any separately authorized staging deployment must verify the
actual deployed SHA and `/status`, then list scoped OMS orders and reconcile
unchanged PAPER balances/PnL against the pre-deployment export. Run the existing
release-readiness process against the deployed revision before claiming new
homologation. Do not reuse the older PASSED bundle as current evidence.

The private Gate SQLite ledger and complete external execution history were
not available in the prior audit. This patch does not close that evidence gap
or establish profitability. No deployment, order, flag, migration, historical
reset or infrastructure operation is part of this change.

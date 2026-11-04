# Gate dashboard alignment — review and rollout contract

## Problem and behavior

The primary dashboard previously opened on legacy Binance/PAPER telemetry even
though Gate.io testnet futures is the selected project venue. The new default
workspace reads Gate status, financial research and closed candles through three
public GET-only bridge routes. Opening the clearly identified legacy workspace
is an explicit action; its market data, account, risk and results remain separate.

The primary backend remains a Binance/PAPER process. This patch does not turn it
into a Gate execution process or copy Gate credentials into it. Its central kill
switch is explicitly described as belonging to that backend, not as a global
Gate control. No Gate execution, account or reset route is added to the bridge.

## Dependencies and source boundary

- This branch is stacked on PR #104, commit
  `1242e2e04d418d3991bd062b5b1156c21a586f7c`. That dependency isolates the older
  backend's OMS reads and command leasing before mapping shared-database rows.
- The Gate service patch in branch `fix/gate-oms-isolation-20261104`, based on
  deployed `767f9e20d59154b435426876b54ecc4aa5fc758f`, adds the companion public
  `/api/gate/market/candles` endpoint and strengthens its own OMS isolation.
- The bridge pins the existing Gate staging HTTPS origin, permits only three
  literal paths, sends GET only, follows no redirects and sends no browser,
  connector or exchange credentials. No main-backend import of `app.demo` occurs.
- Supported chart scope: BTCUSDT, ETHUSDT and SOLUSDT; 15m, 1h and 4h. Charts
  use closed Gate testnet candles. S3 financial entries remain 15m only.
- A missing, failed or wrong-venue response produces an unavailable state, never
  a fallback to Binance, a stale chart, reconstructed trades or fabricated PnL.

If the companion chart route has not been published, the chart is unavailable;
the existing Gate status and financial report can still be read independently.
No deployment is performed by opening this PR.

## Financial presentation

Each market/strategy and each STANDALONE/S1_CONFLUENCE account remains separate.
The workspace shows the source's realized net simulated result, completed trades,
eligible candles, initial capital, candle-close drawdown and cash benchmark.
Open-position exit estimates are labeled separately. No account aggregation,
automatic selection, statistical significance or real-fill claim is made.

The source's minimum candle/trade floors are shown as preliminary sample criteria.
Unavailable financial data is explicitly different from a zero result. Data
freshness not proven by the collector remains visible, even when collection is
recent. The workspace does not infer missing closed-trade cost breakdowns,
profit factor, expectation, stress results or S4/S5 homologation.

## Release evidence

The inherited revision-mismatch handling is strengthened: only equal complete
40-character lowercase hexadecimal commit SHAs can be considered matching.
An invalid/missing revision cannot preserve a declared PASSED state. The original
declaration and bundle identity remain available. Matching SHA identity alone
does not change FAILED/UNSET evidence into an approval.

## Verification

- Full local primary backend suite before the final SHA-format hardening:
  805 passed, 34 skipped. Subsequent focused bridge/status/health/release tests:
  49 passed. CI on the final PR head is the authoritative full-head result.
- Dashboard production TypeScript/Vite build and published contract tests passed.
- Local browser QA used explicitly labeled synthetic fixtures only. Verified
  normal Gate rendering, real chart rendering from the fixture, rejection of a
  wrong-symbol response, clearing the old chart, and explicit legacy navigation.
  Synthetic fixture PnL is not economic evidence and is not committed here.
- Transport regressions cover literal origin, no credential forwarding, blocked
  redirects, malformed/wrong identity, unsupported markets and absence of write
  routes. They do not contact a broker.

## Separate deployment verification

After review, plan the merge order with #104; retarget this stacked PR only once
that dependency is in the deployment branch. Apply the companion Gate patch
through a separately reviewed release. A Gate deployment change restarts its
operational stability window. Preserve the existing S3 journal and frozen policy;
do not rewrite receipts or combine deployments' operational cycle counts.

On an authorized deployment, verify exact published revision, matching release
evidence, primary OMS list HTTP 200 scoped to its own venue/environment, all three
Gate read routes, browser behavior and unchanged disabled execution/release
controls. No orders, flags, credentials, risk, historical financial data, schema,
volumes or retention settings are changed by this proposal. PR #105's legacy
PAPER temporal corrections remain a separate dependency for interpreting that
legacy simulation; Gate S3 uses its independent scorer.

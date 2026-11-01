# S3C — verified read-only candle-close equity valuation

Repository-only research on top of PR #79 (`b2c6847`). No deployment,
Railway modification, restart, service integration, exchange call or order.
S1 remains on `2eb009b`; the S1 audit scheduled for **2026-11-02 04:20
America/Cuiaba (08:20 UTC)** must complete before any release decision.
The three prepared S2E Railway fields remain pending and unchanged.

## Problem and result

The frozen V1 scorecard reports realized PnL and realized-only drawdown. An
open loss could therefore be invisible until its trade closes. A separate
reader now reconstructs the original scorer from an existing journal and
reports open-position hypothetical exit value and candle-close drawdown.
It never changes the frozen policy, risk sizing, old snapshot shape, stored
outcomes, funding convention or journal version. Existing cold recovery
continues to reproduce original outputs exactly.

For each independent symbol × strategy × STANDALONE/S1_CONFLUENCE account:

- Replay reconstructs full-precision entry, quantity and realized equity.
- Estimate exit at the last recorded candle close with adverse slippage.
- Open net estimate = direction × quantity × (estimated exit − entry), minus
  modeled entry + exit commissions and modeled funding.
- Estimated account equity = initial capital + realized net + open net estimate.
- Track peak equity and peak-to-trough decline at recorded candle closes.
- Once closed, charge no open costs: realized PnL already contains those costs.
- Report each account independently, with cash benchmark 0; no portfolio sum.

Synthetic example (not market evidence): a LONG at 100 with quantity 1.25,
zero assumed costs and last close 99 has realized PnL 0, open estimated PnL
−1.25 and equity 998.75. If a prior close marked equity at 1001.25, measured
drawdown is `(1 − 998.75 / 1001.25) × 100`, despite realized drawdown of zero.

## Evidence verification and use

From the backend Python environment:

```python
from app.strategy.gate_s3_equity_valuation import read_only_equity_report

report = read_only_equity_report("/explicit/research-only/s3-forward.sqlite3")
```

The reader requires an existing regular non-symlink file, opens SQLite with
`mode=ro` and `query_only=ON`, and uses a single read transaction (including
committed WAL records). It does not initialize, append, repair or modify the
source database. SQLite may use its normal WAL coordination files; an offline
copy must include a consistent database/WAL snapshot, not just a copied main
file. No export artifact or hosted path is created automatically.

Before returning any report, validate schema, exact frozen model parameters,
per-market hash chain and identity, monotonic observation clocks, deterministic
legacy scoring outcomes, sequence heads and row counts. Missing files, bad
schemas, changed parameters, corruption and missing suffixes fail closed.
Limit: 100,000 observations, inherited from the V1 journal. Hashes detect
accidental/partial corruption, not a privileged rewrite of all evidence.

## Validation and remaining work

Local isolated regression suite: **93 passed, 1 skipped**. This includes 17
new valuation cases covering long/short gains and losses, costs/slippage,
open-equity drawdown, avoiding double charges after close, deterministic repeat
reads, unchanged database bytes and legacy restart results, reading a live
WAL journal while its writer continues, corruption rejection, missing/empty
files and isolation of independent market books. Dedicated PR CI is added;
full monorepo CI must also complete on the new head before release readiness.

This is **not exchange mark-to-market**: use of last OHLCV close cannot establish
current exchange mark price, real fills, liquidation safety or intrabar maximum
drawdown. Every account has explicit source candle and receipt timestamps;
an old journal can yield stale prices. Funding retains the existing frozen
V1 conservative minimum one 8-hour interval, not verified Gate funding.
The source currently evaluates 15m entries with 1h/4h context; this reader does
not establish separate DAY 1h or SWING 4h trading results.

Still required: S1 homologation, staged S2E passive rollout after the release
decision, separately reviewed durable S3 service wiring, fresh prospective
observations, sufficient samples and evidence that confluence improves net
results versus both the baseline and doing nothing. S3C is measurement code,
not proof of profit, promotion or authorization for TESTNET/LIVE orders.

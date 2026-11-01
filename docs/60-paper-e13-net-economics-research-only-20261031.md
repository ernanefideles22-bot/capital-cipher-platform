# E1.3 — PAPER net economics (research only)

## Observed evidence (31 Oct 2026)
The E1.2 PostgreSQL forensic extract identified 13 closed orders, 2 open orders,
15/15 valid decision and risk references, an observed realized net P&L of
-750.0894 quote-denominated units, and profit factor 0.086771. The sample
is too small to establish strategy or agent alpha. No archive reconciliation
or agent causal attribution has been validated.

## Scope
The pure module app.strategy.paper_net_economics_e13 makes bounded,
hypothetical cost-aware calculations using each recorded PAPER entry fill,
planned stop, target, side, and quote notional. It is called only by
paper_e1_readonly_report.py, inside an isolated READ ONLY PostgreSQL
REPEATABLE READ snapshot. It cannot call OMS, agents, adapters, exchanges,
or persist changes.

Scenarios use published **defaults**, not confirmed actual staging execution
settings:
- BASE_NO_VOLUME_IMPACT: taker fee 8 bps each side, 1 bps exit half spread,
  2 bps exit base slippage, 0 bps volume impact.
- STRESS_MAX_VOLUME_IMPACT: same, with 10 bps exit impact.
- This bracket is illustrative; actual participation, gap, market spread,
  funding, adverse selection, latency and quote availability are **unknown**.
  It is *not* a rigorously calibrated upper bound on realistic execution costs.

The existing entry is already a simulated fill. Never charge its adverse
spread/slippage a second time. Fees in this analysis are hypothetically priced
for both legs; they are **not subtracted from historical recorded net P&L**.
Hypothetical net reward/risk is profit-at-target divided by loss-at-stop,
both after scenario costs. The illustrative research screen uses 1.5 net R/R
as a configurable analysis threshold, not an approved runtime risk policy.

The auditor returns aggregate summaries and per-order results under
cost_diagnostics. Each outcome is labeled
SHADOW_RESEARCH_ONLY_NO_EXECUTION. It never filters live orders, changes agent
weights, or replaces previously approved risk checks.

## Release controls
1. Keep PR #67 draft and isolated PAPER backend tests + full CI green.
2. Stage only capital-cipher-db-audit on Railway staging (no branch changes
   to backend, gate, retention, Postgres or Redis).
3. Apply via Railway dashboard 2FA after review.
4. Accept the diagnostic only with E1_AUDIT_COMPLETE, manifest SHA-256 and
   a nontruncated full report; no causal claims from scenario counts.
5. Before *any* operational gate, obtain timestamped venue quotes, full candle
   history, real runtime fee/spread/impact settings and an untouched OOS
   evaluation. A positive backtest, let alone profitability, is not established.

LIVE and TESTNET are not authorized. No volume, migration, retention,
OMS or execution adapter is changed by E1.3.

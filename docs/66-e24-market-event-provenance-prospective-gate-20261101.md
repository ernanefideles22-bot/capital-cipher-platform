# E2.4 — Market provenance, effective sample size preflight, and future-data gate

## Scope

Read-only extension of the existing isolated `capital-cipher-db-audit` staging service.
No new tables, migrations, workers, execution authority or operational backend
changes. Existing `SET TRANSACTION ... REPEATABLE READ, READ ONLY` and
20-second SQL statement timeout remain in effect.

### Verified findings before E2.4 (E2.3 Railway report)

- Up to 900 agent/version/role/symbol/timeframe groups were returned,
  truncated with more groups beyond the limit.
- All returned groups had **25** settled forecast rows: 22,500 settled
  predictions within those groups, zero groups with 30 observations.
- The repeated value 25 is *not* evidence of repeated market opportunities,
  a genuine exchange connection or independent forecasts.
- Previous measured PAPER sample still contains 13 closed orders with net
  P&L -750.0894 in recorded quote units.
- It is not appropriate to select or promote agents from that history.

## What E2.4 measures

1. A bounded seven-day SQL aggregation counts total settled agent forecasts
   **and** distinct market target timestamps by (symbol, timeframe), plus
   distinct agent names and correlations. The sum of distinct targets across
   series is a **descriptive count**, not an effective independent statistical
   sample: simultaneous BTC/ETH/SOL returns are correlated.
2. A separate 90-event recent probe joins target and initial candle timestamps
   to stored `candle_observations` by symbol/timeframe/closed_at, compares
   price with a predefined 1 ppm relative tolerance and checks that the
   candle was received **and recorded** before the corresponding forecast
   or outcome was created. It requires the reference and target observations
   to share an exchange label.
3. A source marked `BINANCE` or `GATE` in database rows is NOT external
   exchange authentication. Synthetic/backfilled data can share the same
   schema. Forecasts themselves lack a direct immutable input-candle source
   ID and explicit exchange; matching records do not establish causal
   access to actual exchange candles.
4. Every query is bounded, with explicit truncation and missing-table
   reporting. The recent 90-event sample is not representative of all data.
   It deliberately does not download raw candle histories or fetch private
   API keys and cannot validate every persisted forecast.

## No premature promotion

- No LIVE, TESTNET, trading or strategy weight update.
- No automatic schedule is set up, and no future unseen-data collection
  is asserted to be running.
- The next genuinely prospective study must freeze agent/version hashes,
  selection criteria, target horizon, venue-specific executable quote
  constraints, cost assumptions and a future cutoff **before** receiving
  post-cutoff data. Preserve candidate HOLD/WAIT and rejected decisions.
- Evaluate both per-candle predictive calibration and independent
  out-of-sample, cost-adjusted, risk-constrained position outcomes.
  Include time clustering and multiple-test correction across ~300 agents.
- A minimum 30 settled forecasts is descriptive only; no statistically
  reliable profit inference follows from crossing that threshold.

## Reproducibility

Module: `backend/app/strategy/paper_market_provenance_e24.py`
Integration: `backend/scripts/paper_e1_readonly_report.py`
Test: `backend/app/tests/test_paper_market_provenance_e24.py`
Railway summary: `market_provenance` in `E1_AUDIT_SUMMARY`.

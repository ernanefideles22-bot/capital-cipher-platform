# Capital Cipher — E1 operational evidence snapshot (2026-10-31)

> **OBSERVATIONAL, NOT AN END-TO-END POSTGRESQL E1 EXPORT.** No testnet/live authorization,
> strategy deployment, agent promotion, OMS write, database mutation, or volume change.

## Source and scope

- Source: Railway service `capital-cipher-backend`, staging, deploy logs, filter
  `Paper order closed` (live read-only inspection on 2026-10-31).
- Window present in returned logs: 2026-10-28 00:00:07 UTC through
  2026-10-30 12:00:01 UTC.
- Deduplication: exactly 12 lines beginning with `Paper order closed`;
  `Audit recorded: PAPER_ORDER_CLOSED` lines are **not** counted as separate trades.
- The PnL amounts are already labeled `pnl` by the engine; do **not**
  re-deduct fees/slippage. Quote-currency/account-balance reconciliation with
  original PostgreSQL records has NOT occurred.
- These 12 are only a visible log subset; completeness beyond this window,
  order identity linkage, underlying strategy timeframes and agent causality
  cannot be established from this log alone.

## Verified observed closing events

| UTC close | Symbol | PnL (logged) | Audited logger exit reason |
|---|---|---:|---|
| 2026-10-28 00:00 | BTCUSDT | -49.4088 | STOP_LOSS |
| 2026-10-28 04:00 | SOLUSDT | -29.4461 | STOP_LOSS |
| 2026-10-28 08:30 | SOLUSDT | +36.8553 | TAKE_PROFIT |
| 2026-10-28 09:00 | BTCUSDT | -33.2678 | STOP_LOSS |
| 2026-10-28 12:15 | SOLUSDT | -32.8214 | STOP_LOSS |
| 2026-10-28 14:00 | ETHUSDT | -109.2242 | STOP_LOSS |
| 2026-10-29 04:00 | ETHUSDT | -218.6075 | STOP_LOSS |
| 2026-10-29 05:00 | ETHUSDT | -70.8599 | STOP_LOSS |
| 2026-10-29 14:45 | BTCUSDT | -91.8594 | STOP_LOSS |
| 2026-10-30 03:45 | BTCUSDT | -44.6183 | STOP_LOSS |
| 2026-10-30 03:45 | SOLUSDT | -103.7831 | STOP_LOSS |
| 2026-10-30 12:00 | BTCUSDT | -37.4627 | STOP_LOSS |

Derived *only from these 12 visible closes*:

- 12 closes: 1 winner, 11 losers, 8.33% win rate, logged net PnL **-784.5039**.
- Gross logged gains **+36.8553**, gross logged losses **-821.3592**;
  observed profit factor **0.044871**.
- BTCUSDT 5 closes / 0 wins / **-256.6170**; SOLUSDT 4 closes /
  1 win / **-129.1953**; ETHUSDT 3 closes / 0 wins / **-398.6916**.
- All 11 observed losses had logger reason `STOP_LOSS`; the sole
  gain had `TAKE_PROFIT`. This does NOT prove where the strategy defect lies.
- Sample is far too small for reliably ranking an agent, strategy, or asset.
  The log is not the canonical accounting source.

## CRITICAL: retention can destroy evidence required for E1

Read-only Railway inspection confirms the deployed cron `capital-cipher-paper-retention`
runs hourly with an attached `capital-cipher-retention-archive-eu` volume
of **1024 MB**. Its 2026-10-31 logs report `hot_hours=6`,
`archive_days=1`, and `archive_files_pruned` of 7 or 5 files in
different runs. Source branch:
`fix/paper-retention-cap-20261015`, files:
`backend/app/maintenance/paper_retention.py` and
`backend/app/maintenance/paper_high_volume_retention.py`.

The implementation archives and DELETEs old rows from `agent_outputs`,
`agent_execution_jobs` / `agent_execution_attempts`, and
`event_journal` (plus forecasts/outcomes and raw market events).
It subsequently **unlinks compressed archives after archive_days**.
By contrast, `paper_orders`, `decisions`, `risk_checks` and
`audit_logs` are not in these retention target lists.

Implications:
1. The 30-day E1 database exporter may recover order/decision/risk/close
   evidence but **not all contemporaneous agent opinions**, since
   `agent_outputs` are removed from the hot database after ~6 hours.
2. One-day archive expiry means older source data may already be
   irrecoverable, unless independently backed up elsewhere.
3. Do **not** blindly increase archive duration on the 1024 MB volume,
   pause retention, or redeploy: that risks filling storage or the DB.
   First measure actual archival rate, free space, and available durable
   backup; then design lossless preservation with verified checksums and
   capacity controls. No existing volume should be deleted.
4. An E1 report MUST label missing archival attribution as
   `EVIDENCE_UNAVAILABLE`, not zero agent activity.

## Next required gates

1. Arrange a **separate read-only role** in the original PAPER PostgreSQL
   and execute `backend/scripts/paper_e1_readonly_report.py` in an
   authorized, isolated environment. Preserve raw JSON and hashes.
2. Verify timestamp and IDs for each order, decision, risk approval,
   closure payload; reconcile net PnL against canonical database and
   archived evidence (if still present).
3. Evaluate strategy-level loss clusters with original point-in-time
   OHLCV and fees/spread/slippage. No causal agent ranking from
   correlation IDs alone.
4. Resolve archive retention capacity and preservation BEFORE trusting
   multiweek agent research data.
5. Only then preregister minimal entry/risk improvements, run
   train/validation/OOS and forward PAPER, and require explicit approval
   for testnet or real-money execution.

This snapshot documents **confirmed failures/risks** and cannot be
used as proof of positive trading expectancy or future profitability.

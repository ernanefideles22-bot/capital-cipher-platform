# E2.3 — SQL-level cohort coverage, historical read-only upgrade

## Confirmed finding from E2.2

Staging audit at commit `74a9ce4` completed with `E1_AUDIT_COMPLETE`.

Bounded sample only:
- 5,000 completed PAPER execution jobs, all `COMPLETED`, 300 distinct
  agent names and 300 distinct agent versions. This **does not** certify
  registry completeness or strategy profitability.
- Job roles: 45 PRIMARY, 4,955 SHADOW; the job sample was truncated.
- 3,000 forecasts, sample truncated (2,970 SHADOW, 30 PRIMARY).
- 1,200 settled forecasts and 1,800 without a visible linked outcome;
  900 agent/version/role/symbol/timeframe groups in this bounded sample,
  none with 30 settled outcomes.
- The limit prevents an assessment of actual historical depth per agent.

## E2.3 scope

E2.3 adds `paper_agent_cohort_e23.py`: a single SQL SELECT that joins
`agent_forecasts` to `agent_forecast_outcomes` using immutable unique
`forecast_id` and matching target/realized timestamp, requiring the
forecast creation clock to precede target. It aggregates over a rolling
seven-day lookback and produces groups by agent/version/role/symbol/timeframe.
The report preserves counts, directional accuracy, mean Brier, a fixed-p=.5
Brier comparison and flags at least 30 settled observations as descriptive
only, not a promotion signal. The full grouping query is capped at 901 rows
and displays at most 25 coverage-sorted groups. If >900 groups exist,
totals and eligibility counts are explicitly **incomplete**. The sample
is historical and correlated, not prospective or independently sealed.

This is a database-side aggregation, not an unbounded download of each
forecast. It uses existing tables only, within the same PostgreSQL
REPEATABLE READ / READ ONLY audit transaction, with statement timeout
and no migrations. It does not run agents or touch OMS/risk or any exchange.
LIVE and TESTNET remain disabled.

**No strategy improves merely because more samples can be viewed.**
No credible claim of agent skill or net profitability is authorized by
this report. For deployment to the existing staging auditor, require
passing CI and operator Railway 2FA, then verify `E1_AUDIT_COMPLETE`.

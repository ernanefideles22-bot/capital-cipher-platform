# E1.1 — Audit-backed PAPER loss baseline and ORM export repair

**Date checked:** 2026-10-31 UTC. **Scope:** Railway backend staging runtime logs.
**Mode:** read-only. **No new orders, no deployment, no PG mutation.**

## Verified aggregate

We queried the `capital-cipher-backend` Railway service's deployment
log stream, filtering the literal runtime message `Paper order closed`,
then parsed the terminal PAPER net-PnL figure. The scoped log search
returned 12 distinct closure messages dated October 28–30. This
independently reconciles the prior database snapshot's reported total.

| Symbol | Closed trades | Wins | Net PnL (PAPER quote currency) |
|---|---:|---:|---:|
| BTCUSDT | 5 | 0 | -256.6170 |
| ETHUSDT | 3 | 0 | -398.6916 |
| SOLUSDT | 4 | 1 | -129.1953 |
| **TOTAL** | **12** | **1** | **-784.5039** |

Gross profitable outcomes: **+36.8553**; gross losing outcomes:
**-821.3592**. Profit factor: **0.044871**; win rate: **8.33%**.
The largest single logged loss was ETHUSDT **-218.6075**.

Closure entries are timestamped between
`2026-10-28T00:00:07.162Z` and `2026-10-30T12:00:01.518Z`.
This is an explicitly limited **12-trade historical sample**, not proof
there were no later trades or that archived logs are complete. The log
events do not independently reveal which agents created each decision,
risk checks, exchange order mode or exact fees. No post hoc agent
profitability ranking is justified.

## Defect discovered in E1 exporter

Original E1 diagnostic used
`AsyncConnection.execute(select(PaperOrderModel)).scalars()`.
That SQLAlchemy Core connection returns the first scalar column
(typically the order ID), **not an ORM `PaperOrderModel` instance**.
Accessing `row.decision_id` would fail for real data even though
synthetic pure-logic tests passed.

E1.1 fixes all five ORM reads by creating `AsyncSession(bind=conn)`
on the PostgreSQL connection, after:
`SET TRANSACTION READ ONLY`; verifies
`SHOW transaction_read_only = on` before any entity query.
The query is bounded by the existing 250-order default, audit/source
integrity and 20-second SQL statement timeout. No changes to the
production service or the database schema are included.

Added a disposable SQLite ORM regression test demonstrating the
incorrect Core scalar behavior and correct bound AsyncSession behavior.
A true read-only PostgreSQL export must still be executed in an
authorized Railway diagnostic session with an appropriate SELECT-only
role; this PR does not have access to a Railway container shell.

## Exact next gate

1. Review E1.1 CI. Keep E1 and E1.1 as draft changes until reviewed.
2. Execute the corrected exporter with an authorized **read-only** DB
   connection to the original staging PostgreSQL; save JSON in a
   protected destination. Never print credentials.
3. Reconcile the 12 known log closures with order IDs, decisions,
   risk checks, closing audit payloads and any retention archives.
4. Separate missing data from verified anomalies. Prioritize any
   invalid brackets, simultaneous opposing same-venue positions,
   and large losses versus planned initial stop risk.
5. Only then design ONE candidate entry-quality change and test it
   against the unchanged baseline outside the calibration sample,
   with real Gate friction and independently managed final holdout.

**No strategy was approved or made profitable by these findings.**

# Capital Cipher — S3B: independent append-only SQLite research evidence and restart recovery

**Status: repository-only research DRAFT. No Railway staging/prod changes. No new service. No capital authorization.**

Base PR #78: `3a38f4872a9a7b812164b3584dba4af687948b9e`. Existing pending Railway patch remains limited to three non-destructive S2E changes for a separate future review/deploy. The Gate S1 24-hour validation remains independent; do not restart it for this work.

## Independent data boundary

- `backend/app/strategy/gate_s3_durable_research.py` persists S3 prospective **public-candle observations and public S2E hypothesis summaries only** into an explicitly supplied, **separate** SQLite file. It does not open/rewrite `research.sqlite3`, `cycles.sqlite3`, any shared Postgres tables, Redis, OM(S), 300 agent registry, account API, or secret.
- Only an allowlisted projection is journaled (symbol, candle OHLCV and original observed receipt timestamp, versioned policy, strategies, decisions, stop/target distances, rejection reasons and explicit no-execution authority); arbitrary S2E fields including API keys never enter the journal.
- SQLite `WAL` and `synchronous=FULL` with event-append transaction, per-market SHA-256 linked hashes, per-symbol last-sequence + count heads, explicit schema version and SQL triggers refusing ordinary UPDATE or DELETE of observations. Each event records **both the source evidence and the full calculated per-arm outcome** (hypothetical open positions, last closed trade entry/exit, stop, target, commissions, funding, PnL and receipt clocks). The hash covers both parts, and cold replay must match the original scoring JSON exactly. The journal binds strategy source, simulator version and frozen risk/fee/slippage/funding assumptions; reopening with a different configuration is blocked. This is **local accidental-corruption evidence** and does not protect against a database administrator rewriting the full file or against physical disk loss.
- Duplicate identical symbol/candle observations return idempotent success without another event. Duplicate conflicting decisions, out-of-order clock, gap/non-monotonic candles, wrong strategy policy, bad scope or executable permission fail closed.
- A process restart recreates the exact in-memory `ShadowForwardScorecard` by **replaying every previously committed observation** in sequence; checks hash chain, row/head count, schema, receipt times and deterministic simulation consistency. Active hypothetical positions, net closed PnL, open risk and decision identity are reconstructed, not assumed.
- Before committing each observation, a temporary scorer must accept it. The event is persisted **before** installing the new in-memory state; on crash between those two steps, restart replay reconstructs it. A second process using a stale journal view is rejected rather than silently merging scores.
- Maximum 100,000 events on cold replay per journal is an explicit safety bound. Larger datasets require a separately reviewed versioned snapshot/compaction approach that retains full append-only evidence. Directory must exist; symlinked data file prohibited; fresh file is chmod 0600.

## What remains unproved

- No application service currently creates or calls this journal. No public status endpoint, automation, or deploy wiring is added by this PR. Its tests are simulated and use isolated temporary database paths; no running Railway database/volume was touched.
- Future service integration must use one dedicated research path on an approved persistent mount, with ownership, disk-space, retention, WAL recovery and health checks. **Do not share existing OMS/order or legacy agent tables or data paths.**
- Recovery checks do not establish genuine historical fills, 24-hour S3 availability, strategy profitability, audited external tamper resistance, accurate liquidation/margin, mark-to-market PnL or multi-instance coordination. Independent backups and crash-consistent WAL handling must be tested in the real runtime before S3 is approved.
- All research scorecard PnL is hypothetical and per independent research arm; benchmark no trade = 0.0. A higher number of indicators or fewer trades is not evidence of edge.
- Existing research script and future locked-policy `FROZEN_AT=2026-11-02T00:00Z` stay unchanged. All old market data remain warm-up, not forward proof.

## Gate before rollout

1. Dedicated S3B CI must pass tests including after-restart equality of simulated PnL, idempotency, append-only triggers, deliberate hash tampering, deleted suffix detection, concurrent writers, secret redaction, missed candles, invalid permissions and S1/S2/OMS regression.
2. Full monorepo CI must pass on this PR.
3. S1 24-hour homologation must pass **independently**, then roll out S2E opt-in SHADOW as already staged and audit actual behavior before another service revision.
4. Next separately reviewed PR can wire S3B into S2E in passive mode, with a read-only status surface and clear startup recovery, but only after release authorization; never enable TESTNET order or LIVE by inference.
5. Accumulate sufficient genuinely fresh market observations after policy freeze to evaluate strategy performance. No automatic promotion/strategy execution.

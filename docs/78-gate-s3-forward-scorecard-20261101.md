# Capital Cipher — Stage 3: prospective SHADOW forward PnL scorecard

**Scope: research-only foundation in PR #78, NOT deployed, NOT connected to OMS, NOT activated in Railway.** Base on S2E PR #77 at source `8f62ed079b73c1d89e570a3ff7b9be45c1d6a58f`. Existing S1 Railway deployment `2eb009b` and all three staged changes to publish PR #77 remain untouched. No new service, infrastructure spending or database changes.

## Why a separate scoring engine

Previous S2A/B/C comparisons used historical OHLCV fetched in batches, where the original network receipt time is unknown. They yielded no trustworthy proof of net alpha. S2D preregistered the S1+S2 policy `8813431` at `2026-11-02T00:00Z`; future data may be evaluated only after the freeze. S2E will allow both stages to inspect the same actual public market snapshots after future controlled deployment.

Stage 3 adds a deterministic, **in-memory, per-(symbol,strategy,arm)** scorecard for experiments comparing `STANDALONE` and `S1_CONFLUENCE` against **cash/no trade PnL = US$0**. No strategy is promoted and no exchange request or order can originate in this module.

## Causal receipt timing

A research signal calculated from candle closed at `t` is only available after the recorded observation/receipt `r>t`. An imagined fill at the immediate opening `t` is **retroactive** and prohibited. For a later fully closed bar `b`, a hypothetical opening fill is permitted only if `open_time(b)>r`; late signals that cannot meet the one-bar eligibility deadline expire. Candle coverage must be exactly contiguous at 15-minute intervals, and observed-at timestamps must increase monotonically. A gap/out-of-order bar halts that market's scorer until an explicit independent reset. Duplicate same-candle observation with identical payload is idempotent; conflicting duplication fails.

## Risk and exit simulation

- Isolated initial equity **US$1,000** per independent symbol/strategy/arm hypothetical account, not pooled into a real portfolio and not connected to private exchange balance.
- Reuses the established S2 cost model (6bps fee and 6bps slippage **per side**, and 1bp funding per modeled 8h interval), max 0.25% equity risk per hypothetical position, 100% notional cap, 2% realized daily loss gate and 10% realized drawdown gate.
- Filled bracket anchored to actual **future simulated bar open**. Gaps through stops assume the adverse open; dual stop/target touch inside the same candle chooses STOP conservatively. Max hold expires at next modeled candle opening.
- `net_pnl`, `gross_after_slippage_before_fees`, `fees` and `funding` are **closed-trade realized simulation values only**. Unrealized PnL and actual margin liquidation/funding times are not modeled. `realized_only_drawdown_pct` is **not portfolio mark-to-market drawdown**. Real fills and profits are unproven.
- Strategy hypotheses are taken only from exactly versioned S2E public-data evidence, matched symbol/decision clock and execution-denial flags. A filtered arm cannot reverse a standalone signal; it may only keep it or WAIT.
- This PR intentionally **does not persist results** or wire the scorecard into the running S1/S2 service. Following a process restart, the in-memory scorecard starts empty; sustained forward performance cannot be claimed until a separate append-only audit journal, restart recovery and controlled deployment have been implemented and approved.

## Mandatory release gates

1. CI of S3 with S1/S2/S2D/OMS regressions and explicit tests for retroactive entry, same-bar dual touch, stop gap, costs, duplicate conflicting signals, time discontinuity, late receipt and frozen policy provenance.
2. Future append-only durable research snapshots with observed receipt clocks and restart-safe replay, separate from OMS and existing research journal, followed by controlled opt-in SHADOW integration *after* S1 24-hour homologation.
3. Demonstrate sufficiently many **new prospective, completely closed** trades after 2026-11-02T00:00Z and compare standalone, S1+S2 filtering and no-trade benchmark; report confidence bands and sensitivity to costs/market regimes.
4. Stage 3 completion and progression to autonomous strategy research must never depend solely on fewer entries, one positive simulated test or a single market regime. No TESTNET/LIVE trading permission may be inferred.

**Never activate OMS writes, modify original 300 agents, reuse or expose API secrets, or make staged changes in Railway from this PR.**

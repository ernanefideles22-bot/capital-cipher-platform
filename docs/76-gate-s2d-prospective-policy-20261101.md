# Capital Cipher — S2D: Frozen forward policy + paginated historical Gate read-only

**Status: development / NOT DEPLOYED / no automatic trading.**

Base: PR #75 S1+S2C confluence at commit `8813431f2d80408241e9befe2a25ab4c5c0245a5`. Railway stays on S1 commit `2eb009b` and continues its independent 24h gate. No changes to deployed Gate service, OMS, PostgreSQL, Redis, volume `/gate-data`, original 300 agents, LIVE/TESTNET order permission or paid infrastructure.

## Why this change

The prior PR #75 comparison used 800 recent 15m bars from a market window previously inspected in S2A and S2B. Although its OOS segment was chronologically later within the sample, it was **not an untouched never-seen future market period**. Its US$ -194.41 → -50.80 difference was still a loss and not proof of alpha.

### Policy preregistration

- Strategy/rules identifier: `S2C_MTF_TREND_RANGE_V1_LOCKED_20261101`
- Frozen tested source commit: `8813431f2d80408241e9befe2a25ab4c5c0245a5`
- Earliest eligible decision candle: **strictly after 2026-11-02T00:00:00Z** (2026-11-01 20:00 America/Cuiaba).
- All policies, fees/slippage/funding estimates, risk assumptions, decision timing and stop precedence remain unchanged. This S2D branch contains data-access, observation, and release-gate logic only.
- Any change to the policy must be versioned and gets a NEW future freeze; do not backdate or rewrite this policy ID.

### Public historical Gate retrieval

Official Gate API v4 public futures candlesticks endpoint supports UTC `from` and `to`, which are **mutually exclusive** with `limit`:

https://www.gate.com/docs/developers/apiv4/en/futures/

`gate_s2d_history.py` uses only `GET /api/v4/futures/usdt/contracts/{symbol}` and `GET /api/v4/futures/usdt/candlesticks` on the **existing** `https://api-testnet.gateapi.io` domain. It fetches <=500 bars per chunk, <=6000 bars per bounded invocation, with exact aligned close-time windows and strict discontinuity, gap, duplicate, OHLCV validity, metadata and future-candle rejection. Public GET only; no keys/private endpoints. Historical chunks use their *requested close boundary* for completeness validation, never fake that this was the original network receipt time.

One-shot test probes 1250 historical 15m bars (3 pages) and 180 1h + 180 4h bars per market, BTC/ETH/SOL. It reports status, not profitability. It runs in the GitHub Actions PR CI only and does not schedule future execution or consume new Railway services.

### Frozen forward evaluator

- Requires synchronized 15m, 1h, 4h Gate Futures candles, real batch-observation receipt timestamps, consistent OHLCV continuity and adequate warm-up prior to the freeze.
- Rejects bars dated after observation and stale current-market snapshots. If no new future candles have closed, returns `PRECOMMITTED_WINDOW_NOT_STARTED`; if fewer than 96 newly closed 15m candles, `INSUFFICIENT_NEW_CLOSED_BARS`; if pre-freeze history has rolled out, `PRE_FREEZE_WARMUP_EVICTED_OR_MISSING`.
- The earliest eligible strategy decision must occur on a bar **closed strictly after the policy freeze**, and simulated entry only occurs at the **NEXT bar's open**. Historical S1 higher timeframe context contains only candles closed before that decision. Confirmed pivot lookahead is forbidden.
- Independently compares three frozen strategy policies both standalone and with S1 confluence gating; includes zero-trade `0` PnL benchmark, same costs/position sizing/ATR stops, and OOS drawdown/number of completed trades.
- A single strategy needs at least 30 *completed* filtered trades even to be a preliminary, descriptive sample, not to be released. All results always carry `profitability_proven=False`, `automatic_promotion_authorized=False`, `order_submission_authorized=False`.
- Prospective preregistration does **not** imply live-recorded API receipts, fill-price accuracy, independent markets, final multiple-hypothesis adjustment, realized profit, or permission to trade.

### Next gate

One-shot range tests and regression must pass. The actual **post-freeze future period has not yet been observed** at preparation time. Do not claim completion of S2 or profitable confluence, and do not deploy S2 to Gate without an explicit separate approval after reviewing new evidence. For sufficiently long data horizons, rerun on data strictly later than the frozen cutoff with consistent versioned code and bounded pagination; do not silently switch to another policy or use new windows to optimize old rules. No recurring automation is enabled by this PR.

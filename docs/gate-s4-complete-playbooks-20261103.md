# S4: complete fixed breakout playbooks (2026-11-03)

Research only. No runtime import, OMS integration, order write, merge, deployment, canary or risk-policy change. S3 continues separately. These deterministic hypotheses are not autonomous AI agents.

## Frozen rules (version 1.0.0)

| Rule | BREAKOUT_RETEST_V1 | FAILED_BREAKOUT_V1 |
|---|---|---|
| Context | Prior 20-bar channel, EMA20/50 aligned, breakout volume >=1.2x prior mean | Prior 20-bar range width 2–8 ATR, pre-breakout EMA20/50 spread <=0.5 ATR |
| Trigger | Close outside channel by 0.1 ATR | Close outside channel by 0.1 ATR |
| Confirmation | 2–6 bars later, separate intervening retest within 0.25 ATR; subsequent closes hold the level within 0.25 ATR; confirming directional candle and close | 1–2 bars later, directional return inside old range by 0.1 ATR; midpoint target still offers >=1.25R |
| Stop | Beyond retest extremes by 0.25 ATR, at least 0.5 ATR away | Beyond false-break extreme by 0.25 ATR |
| Entry risk | Stop distance 0.5–3 ATR; entry drift <=0.5 ATR | Same |
| Target | 2R from executed entry | Frozen prior range midpoint; executed reward >=1.25R |
| Expiry / hold / cooldown | 8 / 20 / 4 bars | 5 / 12 / 4 bars |
| Thesis invalidation | Closed price across original breakout level | Closed price back beyond failed-break level |

Long and short rules are mirrored. Entries occur at bar OPEN two indices after the decision close, so execution is strictly later than information availability; the intervening complete bar can invalidate the setup. Stress delay adds one bar. A setup is consumed on its first attempt, even when entry is rejected. Targets/stops already touched before entry prevent entry. Stop wins ambiguous OHLC bars. Existing gap brackets precede time/thesis exits; time/thesis exits execute at a later OPEN. No same-bar reentry. End-window marks are costs-inclusive estimates, excluded from completed-trade evidence.

Risk remains the existing ResearchRisk: 1000 initial equity, 0.25% per trade, 1x notional, 2% daily loss gate and 10% realized drawdown gate. Fees, slippage and funding are modeled, not historical account fills. Nominal: 6/6 bps per side and 1 bp/8h funding. Stress: 12/12 bps and 2 bp/8h; third scenario adds an entry bar. Research uses OHLC, not order-book execution or intrabar paths.

## Registration and selection

Before financial replay, freeze source fingerprint, source archives, all 18 attempts (2 hypotheses x 9 series), criteria, risk and statistics in an append-only SHA256-chained SQLite journal. No parameter grid, retries or reset. Retain failures and skipped validations. Budget is 18 train attempts, at most one training winner per market/timeframe, followed by two separate validation folds for selected candidates only.

Data: previously archived production-public Gate BTC/ETH/SOL USDT perpetual candles, 6000 per 15m/1h/4h series (54,000 total). Raw response pages and source hashes are verified. Training indices [220,3600); candidate validation [3625,4200), [4225,4800). Final [4800,6000) is not replayed by this runner. Previously used Testnet study periods overlap part of that reserve: only [5400,6000) remains the original globally reserved segment. No claim of globally untouched out-of-sample validation: earlier project research already inspected training and validation dates. The present selection itself uses training only.

Every cost scenario must have >=30 completed trades, positive closed and marked net PnL, profit factor >=1.2, largest win share <=50% and candle-close drawdown <=5%. Select the highest worst-scenario train marked net; policy ID breaks ties. Same criteria apply independently to each later validation fold.

Cash is the only statistical benchmark. Old parent strategies use different exits and timing, so this study does not claim a causal improvement versus those controls. Family: 54 slots (2 x 9 x 3), retaining unselected slots as NOT_EVALUATED with p=1. Selected validation uses the existing stationary bootstrap, 9999 replications, sensitivity to mean blocks 8/16/32 and Holm adjustment. A simultaneous one-sided Monte Carlo precision upper bound (.05/162 allocation) must also pass Holm. This only controls Monte Carlo uncertainty conditional on bootstrap assumptions; stationarity/mixing and exact finite-sample validity are not established. Earlier project searches are not fully corrected. Statistical passage alone cannot authorize reserve access, promotion or orders.

## Registered real-data result

All 18 training attempts completed, no source/engine failures. Zero selected: later validation was therefore skipped for all 18, without attributing fictitious validation losses. All 18 had negative closed net under stress plus delay; 14 had negative nominal closed net. Seventeen lacked 30 completed trades in at least one scenario. Nominal positives were BTC1h retest, ETH4h retest, SOL1h failure and SOL4h failure; none met all frozen gates. These are training observations, not performance claims.

No prospective candidate or tracking was started. Do not tune thresholds until something passes on these same dates. Next research requires a separately registered mechanism/data rationale and fresh prospective evidence; the two versions in this experiment remain rejected by the training gate.

## Reproduction and tests

From backend, run `python -m scripts.gate_s4_complete_playbooks register --root NEW_ROOT --catalog ARCHIVE/study/history/catalog.json --output manifest.json`, then `run --root NEW_ROOT --output review.json`. Registration is exclusive; keep the original source version. `review` is read-only. For relocation use `--source-root ARCHIVE/study/history` on run. A fresh reproduction is a reproduction, not an independent discovery.

The dedicated workflow tests hand-labelled long/short setups, strict clocks, barrier ambiguity, costs, cooldown and consumed setup behavior; it also tests training-only selection, every gate, phase ordering, no retry, journal mutation rejection, Monte Carlo binomial oracles, unselected validation blocking, selected fold boundaries, and existing S2/S3/S4 regressions. Software tests establish implementation behavior, not trading profitability.

# S4: one slower momentum hypothesis, known training only

One registered version: `TSMOM_20D_4H_COST_BUDGET_V1` (1.0.0), BTC/ETH/SOL USDT perpetuals on 4h only. No parameter grid, extra instruments or timeframe search. It is derived after prior failed project studies, not an independent discovery. New isolated modules preserve every legacy strategy, research fingerprint and journal, S3, risk and OMS. No runtime wiring, order authorization, merge or deployment.

## Economic rationale and limitations

Own-price momentum could reflect persistence and gradual information/attention effects. A slower daily decision with a wider movement objective might leave more room for costs. This is a testable rationale, not a predicted return. AQR's Time Series Momentum research studies much longer lookbacks and diversified non-crypto futures; Liu/Tsyvinski study cryptocurrency-specific momentum. Neither validates this exact 20-day rule, SOL, these Gate contracts, stops, targets or present performance.

Primary sources:
- https://www.nber.org/papers/w24877
- https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum

These sources motivate a hypothesis; they are not an endorsement or a backtest of this implementation.

## Frozen rule

At each fully closed 00:00 UTC 4h candle, compute own log return over 120 bars (20 days). Normalize its absolute value by the population standard deviation of those 120 past log returns times sqrt(120). Require normalized magnitude >=1 and nondegenerate volatility. Positive direction proposes LONG; negative proposes SHORT. Warmup: 220 bars. Decisions elsewhere WAIT. All inputs are closed and known, with stale/future clocks refused.

Stop: 2 ATR14 from the decision close. Target: 3R (6 ATR at decision). Hold at most 30 bars (five days), cooldown six bars, setup expiry three bars. Thesis invalidates if the rolling 120-bar log return changes sign or becomes zero. Exit that closed observation at a strictly later OPEN. Entries execute two bar indices after decision (one intervening full bar); extra-delay stress adds one more. Pending setup stops/targets already touched cancel entry. Entry drift must be <=0.5 ATR, executed stop distance within 0.5–3 ATR. Recompute 3R target from the executed entry and absolute stop. Nonpositive price geometry is refused.

Affordability is a fixed proxy: 2*(12 fee +12 slippage) bps +15*2 funding bps =78 bps of entry notional. Require proxy <=20% of planned target fraction, both at proposal and at actual entry. Fifteen 8h funding intervals cover the 120h maximum hold. It is a planning assumption, not a realized cost bound or expected profit; gaps and fees at exit prices can differ. Daily opportunities and longer holds do not guarantee lower realized turnover or cost than unrelated policies.

Existing research risk remains 1000 initial equity, 0.25% stop risk per entry, <=1x notional, 2% daily-loss and 10% realized drawdown entry gates. Gap losses and costs can exceed planned stop risk. Worst-case stop wins ambiguous candles; existing gap brackets precede time/thesis exits; no same-bar reentry. Forced end-window estimates are excluded from completed-trade evidence.

## Bounded research protocol

Register source fingerprints, rules, thresholds, source hashes and three attempts before the first financial replay. Append-only SHA256-chained SQLite journal, exclusive registration and no retries/reset. Budget: three markets x three fixed scenarios, nine replays. Nominal costs: 6 fee/6 slippage bps per side and 1 funding bp/8h; stress doubles those; third scenario adds an entry bar. These are modeled assumptions.

Input is the already used production-public nine-series Gate archive. Only the three 4h series enter this study. Replay [220,floor(.6*N)), i.e. [220,3600) for each 6000-bar series. Later 40%, including [4800,6000), receives no financial evaluation. Data integrity checks hash complete input files; they are not financial replay of later prices. Original globally reserved [5400,6000) remains unassessed by this runner. Past project training and validation searches are disclosed: no globally new historical evidence or full correction for those searches is claimed.

Keep prior criteria: >=30 completed trades in every scenario, closed and marked net positive, PF>=1.2, largest-win share<=50%, candle-close DD<=5%. A training passage would only warrant a separately pre-registered future validation protocol. This runner cannot open validation/reserve, compute statistical significance, start prospective tracking, or promote to S5, even when training passes. No extra parameter search is authorized after rejection.

## Recorded result

All three attempts and nine scenarios completed, no technical/data failures. All scenarios met the 30-completed-trade floor. Zero passed the financial gate: closed net and profit factor failed in all cases. Nominal:

| Market | Closed trades | Net USDT | Profit factor | Raw reconstructed gross before modeled costs |
|---|---:|---:|---:|---:|
| BTCUSDT | 38 | -25.543 | 0.611 | -14.592 |
| ETHUSDT | 42 | -16.173 | 0.728 | -8.697 |
| SOLUSDT | 44 | -20.847 | 0.708 | -14.351 |

Capital is 1000 USDT independently per market/scenario; this is not a portfolio return. Reconstructed gross uses each scenario's executed trades and fixed exits; it is not a fresh zero-cost strategy simulation. Stress plus delay nets: BTC -38.954, ETH -37.760, SOL -27.227. This version remains rejected. No validation or prospective profitability result exists.

## Tests and reproduction

Tests cover independent return and cost arithmetic, long/short mirroring, daily decision times, future-tail independence, stale clocks, invalid sources/timeframes, strict entry clocks, stop-first ambiguity, pre-entry rejection, rolling thesis exits, holding limits, gaps exceeding planned risk, affordability recheck, costs, delay, cooldown, consumed setups and excluded boundary marks. Journal tests cover all three failed attempts, phase order, exclusive registration, no retry, source fingerprint changes and training passes that cannot open future validation. Existing S2/S3/S4 regressions remain required.

From backend:
```
python -m scripts.gate_s4_slow_momentum register --root NEW_ROOT --catalog ARCHIVE/history/catalog.json --output manifest.json
python -m scripts.gate_s4_slow_momentum train --root NEW_ROOT --source-root ARCHIVE/history --output review.json
```
Keep this exact source version. A reproduction on known dates is not another discovery. `review` is read-only; never reset an existing journal.

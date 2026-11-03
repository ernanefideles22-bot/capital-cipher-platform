# S4: fixed descriptive signal/entry/exit diagnostics

This follows #92's failed momentum training. It adds no strategy, parameter grid, order path, S3 wiring, changed risk or promotion. No merge or deployment. All previous sources and research fingerprints/journals remain intact.

## Registered scope

One SHA256-chained, append-only three-event journal: REGISTERED -> STARTED -> RESULT or FAILED. No retry/reset. Pin the completed three-market parent journal head and bytes, input/archive hashes, implementation fingerprint, and diagnostic protocol before calculating these diagnostic outcomes. Parent losses were already known; this is post-hoc descriptive investigation, not a newly independent financial test.

Three cases (BTC/ETH/SOL 4h), three cohorts and fixed horizons 6/12/30 bars: 27 descriptive summary slots. New strategy trial budget is zero. No exit/entry threshold is selected, no significance test is performed and no statistical approval is implied.

## Independent raw mapping

Do not call the existing normalizer. Reconstruct a timestamp dictionary directly from archived public responses: canonical close time = raw t+14400; o/h/l/c map directly; normalized base volume = v times archived contract multiplier. Check every raw-page SHA, origin, endpoint, symbol/timeframe, duplicate/misaligned timestamps, all prices/volumes and exact receipt clocks against canonical candles. Gate's official API defines the fields (https://www.gate.com/docs/developers/apiv4/en/futures/). This verifies the archived transformation, not independent second-venue prices or all historic contract multiplier changes. Raw archives themselves are not prospectively received observations from the historic trade date.

All 6000 source candles per selected market can be checked for integrity, including reserved data. Price-outcome diagnostics are strictly limited to the known training prefix [220,3600) and never compute later/reserve outcomes.

## Event outcomes

Use the frozen #92 reader. For every non-WAIT signal at closed index i, the diagnostic raw entry is OPEN i+2, strictly later than decision availability. H-bar mark is CLOSE entry+H-1. Require a complete 30-bar horizon before training end; exclude the signal entirely if incomplete, so horizon sample composition is identical. Never use later history to fill incomplete outcomes.

Report signed price change in bps and the same-event-time always-long price change. Their difference is descriptive: exposure, risk and costs are unmatched. This is neither a portfolio nor a tradable PnL comparison. Raw marks omit stop/target, fees, slippage, funding, fills, cooldown, position overlap and risk controls. Signals rejected from actual trading remain in ALL_SIGNALS and must not be presented as executed trades.

Cohorts:
- ALL_SIGNALS, with overlapping outcome windows explicitly disclosed.
- NONOVERLAPPING_SIGNALS: chronologically take the first eligible signal and greedily skip signals whose entry index falls inside that signal's 30-bar window. No offset/grid selection. Nonoverlap does not establish independence/stationarity, and markets remain correlated.
- EXECUTED_NOMINAL_TRADES: match the original nominal entry decision, side and exact i+2 index. No new replay, optimization or trade selection from outcomes.

## Existing trade paths

Reconcile archived nominal entry against raw OPEN plus the frozen 6bps slippage. Invert exit slippage to reconstruct raw gross on that executed path, then deduct slippage, recorded fees and funding to match recorded net. This is not a fresh zero-cost strategy simulation: changing costs might alter fills and subsequent trades.

Compute favorable/adverse excursions only from full bars after entry and before the exit candle. Exclude exit-candle high/low because they may occur after exit. These are incomplete observable-bar excursion measures, not exact tick-path MFE/MAE. Later fixed-horizon marks after an actual stop are retrospective counterfactual price paths, not realized profit, proof of a better stop, or admissible entry features.

## Findings

All 18,000 selected-source candles and 39 raw pages reconcile. There are 508 complete-horizon signals (one additional ETH signal excluded), 152 greedy nonoverlap signals and 124 actual nominal trades across the correlated markets. No new strategy was tested or approved.

At the five-day mark, all-signal directional means in bps: BTC -22.79, ETH +2.05, SOL -84.37. For the original executed entries: BTC -34.21, ETH -108.68, SOL -13.42. ETH has some positive shorter-horizon cells; reporting the entire fixed table prevents selecting one as evidence of profitability. None includes a tradable cost/risk model or independent validation. The data do not support attributing all losses solely to the stop, and do not identify a unique causal defect.

Research decision: keep #92 rejected; inspect/reformulate predictive signal before testing another exit parameter. Any rule derived here is a new searched hypothesis and must be budgeted, registered and confirmed on independent evidence. Repeated historical adjustment is subject to backtest overfitting (Bailey et al.: https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). This report does not estimate that paper's PBO statistic.

## Reproduction and tests

From backend, with the exact published source version:
```
python -m scripts.gate_s4_momentum_diagnostics register --root NEW_ROOT --parent-root PARENT/study --output manifest.json
python -m scripts.gate_s4_momentum_diagnostics run --root NEW_ROOT --parent-root PARENT/study --source-root ARCHIVE/history --output diagnosis.json
```
The parent is the #92 momentum evidence journal, not a regenerated journal with a different registration time. A relocated path is accepted only if its parent head and exact bytes still match. `review` is read-only. Reproduction is not new evidence.

Tests independently exercise raw-field corruption (even rehashed), exact horizon arithmetic, boundary exclusion, short/long signs, nonoverlap selection, future-tail independence, matching recorded clocks, reconstructed costs and exit-bar exclusion. Registry tests cover parent tampering, missing inputs preserved as failure, exclusive creation, no retry, phase order, protocol changes and append-only triggers. Existing S2/S3/S4 regression tests remain required.
